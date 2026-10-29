//! Fetching one remote subscription, in one format.
//!
//! ## Why the format is forced by path, not by User-Agent
//!
//! The remote panels are Marzban-family and expose `/sub/<token>/<client_type>`,
//! which makes *them* do the format conversion. That is what lets this feature
//! support every output format without this poller ever parsing a proxy URI.
//!
//! ## Why the path segment is discovered instead of hard-coded
//!
//! Verified live against a registered subscription: the panels are **not all
//! the same fork**, and their client_type vocabularies differ. One returned
//!
//! ```text
//! 422 {"detail":{"client_type":"Input should be 'links', 'links_base64',
//!      'xray', 'wireguard', 'sing_box', 'clash', 'clash_meta', 'outline' or
//!      'block'"}}
//! ```
//!
//! where this panel's own fork would have accepted `v2ray` / `v2ray-json` /
//! `sing-box` / `clash-meta`. Rather than parse that error message (brittle) or
//! add a per-row "flavour" setting (a knob the operator would have to know the
//! answer to), each canonical format carries an ordered candidate list. The
//! segment that returns 200 is remembered in Redis for a day, so the probing
//! cost is paid once per subscription per format and a remote panel that
//! changes vocabulary re-discovers itself automatically.
//!
//! ## Why a 200 is not success
//!
//! Two rejection shapes exist in this family, both observed:
//!
//! * `403 {"detail":"HWID header required"}` — the newer panel.
//! * **`200` with an empty body** and the reason in a `profile-title` header —
//!   this panel's own fork (`_announcement_response`), used for device blocked,
//!   device-limit reached and non-Happ client. A status-code check alone would
//!   read that as success and overwrite good hosts with nothing.
//!
//! So every response is shape-checked, and anything that fails leaves the last
//! good payload in place.

use std::time::Duration;

use base64::Engine;

/// Canonical format names — these are the panel's own names and they are what
/// the Redis keys use (`app/utils/cache.py`). Each maps to the ordered list of
/// path segments to try on the remote panel.
///
/// `outline` is deliberately absent: it is a single-key config with no list to
/// extend, and `outline_enabled` is mutually exclusive with `device_limit`, so
/// no device-limited user can ever receive it.
///
/// SAFETY INVARIANT: only the `v2ray` payload is host-filtered (unroutable and
/// domestic `.tm` hosts dropped) — that filter is line-based and the other
/// formats are JSON/YAML documents this poller does not parse. The panel merges
/// `v2ray` only (`app/subscription/external.py: MERGEABLE_FORMATS`), so nothing
/// unfiltered can reach a user today. Anyone teaching the panel to merge another
/// format must add filtering for it here first.
pub const FORMATS: &[(&str, &[&str])] = &[
    // Fetched first and the only one the panel merges today; a failure here is
    // what actually matters. BARE_URL is last: not every remote is Marzban.
    // Verified live against a Happ subscription server (nginx + Express, not
    // Marzban at all) where EVERY client_type segment 404s and only the base URL
    // serves anything — without this candidate that panel can never be mirrored.
    // Only `v2ray` gets it: the bare URL returns whatever format the remote picks
    // from our User-Agent, which with a Happ UA is a URI list. Storing that under
    // `clash` or `xray` would file a URI list as YAML/JSON.
    ("v2ray", &["links_base64", "v2ray", "links", BARE_URL]),
    ("v2ray-json", &["xray", "v2ray-json"]),
    ("clash-meta", &["clash_meta", "clash-meta"]),
    ("clash", &["clash"]),
    ("sing-box", &["sing_box", "sing-box"]),
];

/// The one format whose failure actually matters.
///
/// It is the only one the panel merges (`app/subscription/external.py:
/// MERGEABLE_FORMATS`), so if it succeeds the users served by this subscription
/// have everything they are going to get. The other formats are mirrored ahead
/// of need, and remote panels differ wildly in which they offer — measured on
/// one live panel: only `xray` and the bare URL answer at all, `clash` /
/// `clash_meta` / `links` return 406 (it gates the format on User-Agent) and
/// `sing_box` returns 500. Treating any of that as an error made a perfectly
/// healthy subscription serving 30 hosts show a red failure in the panel.
pub const ESSENTIAL_FORMAT: &str = "v2ray";

/// Candidate meaning "fetch the base URL itself, appending no path segment".
///
/// Deliberately not the empty string: an empty candidate is indistinguishable
/// from "nothing cached" in the dialect cache, so the fallback would be
/// re-probed (and its predecessors re-404'd) on every single cycle. The value
/// starts with `@`, which no path segment in this family uses.
pub const BARE_URL: &str = "@bare";

/// User-Agent presented to every remote panel. Keeps a real Happ shape
/// (`Happ/<version>/<platform>/<id>`) because panels in this family gate on the
/// client being Happ, while naming itself in the id field so the operator can
/// recognise it in the remote panel's device list and logs.
pub const USER_AGENT: &str = "Happ/5.2.0/ios/micky-subs-poller";

/// User-Agent used to retry a BARE-URL fetch of the URI-list format.
///
/// The bare URL is an auto-dispatch endpoint: the remote picks the format from
/// our User-Agent. Advertising Happ is what several panels gate on, but a modern
/// Happ version also tells a Marzban-family panel to serve Xray-JSON — measured
/// on a live remote: every `Happ/*` UA returned 13.9 KB of JSON, while
/// `v2rayNG/1.8.0`, `Streisand/48` and a plain unknown UA all returned the same
/// base64 URI list. An unknown UA is the reliable choice because it falls
/// through every client-specific branch to the default.
///
/// Tried only AFTER the Happ UA, and only for the bare URL: one panel already
/// mirrors correctly on Happ, and this must not change what already works.
pub const NEUTRAL_USER_AGENT: &str = "micky-subs-poller/1.0";

/// `X-Hwid` used for registry rows that set none of their own.
///
/// This is the operator's OWN HWID, already registered as a device slot on the
/// remote integration users. That matters: on panels enforcing a device limit an
/// unknown HWID CLAIMS a slot on first use, and in this family a claimed slot is
/// released only by an admin block — never by time. A self-describing value like
/// "micky-subs-poller" would have been easier to spot in the remote device list,
/// but it would have burned a slot on every remote user the first time a row was
/// registered without its own hwid. An existing slot matches instead.
///
/// Per-row `hwid` still wins; this is only the fallback.
pub const FALLBACK_HWID: &str = "e44af9cea1453e4c";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(20);

/// Largest response body read from a remote panel: 2 MB.
///
/// Payloads land in a Redis capped at 64 MB with `allkeys-lru`, shared with
/// the panel's own caches (announcements, device blocks, admin mappings).
/// Unbounded, one remote answering with a huge body would evict those, and
/// the other subscriptions' payloads with them. Real subscriptions are tens of
/// KB. A bigger body is refused like any other bad fetch, so the last good
/// payload stays in place.
pub const MAX_BODY_BYTES: usize = 2 * 1024 * 1024;

pub fn build_client() -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        // A remote panel behind a redirect to an HTML login page must not look
        // like a subscription; the shape check would catch it anyway, but not
        // following redirects keeps the failure honest and fast.
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(USER_AGENT)
        .build()
        .unwrap_or_else(|_| reqwest::Client::new())
}

#[derive(Debug, Clone)]
pub struct Fetched {
    /// Validated, render-ready payload (decoded, for `v2ray`).
    pub payload: String,
    /// The path segment that worked, to be cached as this remote's dialect.
    pub segment: String,
    pub http_status: u16,
    /// The remote user's own quota/expiry, verbatim. This is the early warning:
    /// when that user expires or caps out, every local user loses these hosts at
    /// the same moment.
    pub userinfo: Option<String>,
}

#[derive(Debug)]
pub struct FetchError {
    pub http_status: Option<u16>,
    pub message: String,
    /// Decoded `profile-title` of an announcement response (the empty-200
    /// rejection shape). Carried up because for the panel family this header
    /// is the only place a fetch says WHY it was refused — and one class of
    /// reason (see `is_remote_dead_title`) means the whole account is dead,
    /// not just this request.
    pub rejection_title: Option<String>,
}

impl FetchError {
    fn msg(message: impl Into<String>) -> Self {
        FetchError {
            http_status: None,
            message: message.into(),
            rejection_title: None,
        }
    }
}

/// The exact `profile-title` strings a panel of this family sends when the
/// subscription's USER is non-active — mirrored from STATUS_ANNOUNCEMENT_TK
/// in app/routers/subscription.py; change both together. Once a remote panel
/// carries that gate, a lapsed account stops answering with hosts + a
/// `subscription-userinfo` header (which the quota/expiry check in mod.rs
/// would catch) and starts answering with an empty 200 + one of these titles,
/// so matching them is what keeps dead-account detection working at all.
///
/// Deliberately NOT matched: the device-level refusals ("Blok Edildiniz",
/// "Enjam çägi doly — admin bilen habarlaşyň", "Diňe Happ ..."). Those say
/// OUR fetch identity was rejected — the account behind the subscription may
/// be perfectly alive, so its last-good payload must stay (bounded by the
/// payload TTL), not be deleted.
pub const REMOTE_DEAD_TITLES: &[&str] = &[
    "Maglumat çägi gutardy",   // limited — data cap reached
    "Abonement möhleti tamam", // expired
    "Hasap öçürildi",          // disabled
    "Garaşmakda",              // on_hold — never started
];

pub fn is_remote_dead_title(title: &str) -> bool {
    REMOTE_DEAD_TITLES.contains(&title.trim())
}

/// Decode a `profile-title` header value. The panel family sends
/// `base64:<b64>` (share.py `encode_title`); anything else is passed through
/// as-is so an unknown panel's plain-text title still reads in logs.
pub fn decode_profile_title(raw: &str) -> String {
    let raw = raw.trim();
    match raw.strip_prefix("base64:") {
        Some(b64) => base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .ok()
            .and_then(|bytes| String::from_utf8(bytes).ok())
            .unwrap_or_else(|| raw.to_string()),
        None => raw.to_string(),
    }
}

/// Fetch one format, trying the cached dialect segment first.
pub async fn fetch_format(
    http: &reqwest::Client,
    base_url: &str,
    hwid: &str,
    canonical: &str,
    cached_segment: Option<&str>,
) -> Result<Fetched, FetchError> {
    let candidates = candidates_for(canonical, cached_segment);
    let mut last = FetchError::msg(format!("no candidate segment for {canonical}"));

    for segment in candidates {
        // The bare URL dispatches on User-Agent, so a rejected body there may
        // just be the wrong FORMAT rather than a real failure — retry it once
        // with a UA that maps to a URI list.
        let uas: &[Option<&str>] = if segment == BARE_URL {
            &[None, Some(NEUTRAL_USER_AGENT)]
        } else {
            &[None]
        };
        for ua in uas {
            match try_one(http, base_url, hwid, canonical, &segment, *ua).await {
                Ok(fetched) => return Ok(fetched),
                Err(e) => {
                    tracing::debug!(
                        "{base_url}/{segment} ({canonical}, ua={}) rejected: {}",
                        ua.unwrap_or("default"),
                        e.message
                    );
                    last = e;
                }
            }
        }
    }
    Err(last)
}

/// Candidate segments in the order to try them: the remembered one first, then
/// the rest of the canonical list (minus a duplicate of the remembered one).
fn candidates_for(canonical: &str, cached_segment: Option<&str>) -> Vec<String> {
    let known = FORMATS
        .iter()
        .find(|(name, _)| *name == canonical)
        .map(|(_, c)| *c)
        .unwrap_or(&[]);

    let mut out: Vec<String> = Vec::with_capacity(known.len() + 1);
    if let Some(seg) = cached_segment {
        if !seg.is_empty() {
            out.push(seg.to_string());
        }
    }
    for seg in known {
        if !out.iter().any(|s| s == seg) {
            out.push((*seg).to_string());
        }
    }
    out
}

async fn try_one(
    http: &reqwest::Client,
    base_url: &str,
    hwid: &str,
    canonical: &str,
    segment: &str,
    user_agent: Option<&str>,
) -> Result<Fetched, FetchError> {
    let url = if segment == BARE_URL {
        base_url.trim_end_matches('/').to_string()
    } else {
        format!("{}/{}", base_url.trim_end_matches('/'), segment)
    };
    let mut req = http.get(&url).header("X-Hwid", hwid);
    if let Some(ua) = user_agent {
        req = req.header(reqwest::header::USER_AGENT, ua);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| FetchError::msg(format!("request failed: {e}")))?;

    let http_status = resp.status().as_u16();
    let userinfo = resp
        .headers()
        .get("subscription-userinfo")
        .and_then(|v| v.to_str().ok())
        .map(String::from);
    let profile_title = resp
        .headers()
        .get("profile-title")
        .and_then(|v| v.to_str().ok())
        .map(decode_profile_title);

    if http_status != 200 {
        // 4xx here is the normal signal that a candidate segment is wrong (422)
        // or that the HWID was rejected (403) — both belong to the caller's
        // decision, so carry the code up. The title rides along too: no panel
        // in this family sends a dead-status title on a non-200 today, but one
        // that did must not dodge dead-account detection on a technicality.
        // Same cap for an error page; if even that is too big, the cap's
        // message stands in for its first line.
        let body = read_body_capped(resp, MAX_BODY_BYTES)
            .await
            .unwrap_or_else(|e| e);
        return Err(FetchError {
            http_status: Some(http_status),
            message: format!("HTTP {http_status}: {}", first_line(&body, 160)),
            rejection_title: profile_title,
        });
    }

    let body = read_body_capped(resp, MAX_BODY_BYTES)
        .await
        .map_err(|m| FetchError {
            http_status: Some(http_status),
            message: m,
            rejection_title: profile_title.clone(),
        })?;

    let payload = validate(canonical, &body).map_err(|m| FetchError {
        http_status: Some(http_status),
        message: match profile_title.as_deref() {
            Some(title) if !title.is_empty() => format!("{m} (profile-title: {title})"),
            _ => m,
        },
        rejection_title: profile_title.clone(),
    })?;

    Ok(Fetched {
        payload,
        segment: segment.to_string(),
        http_status,
        userinfo,
    })
}

/// Shape-check a 200 response and normalise it into what gets stored.
///
/// This is the guard against the 200-with-empty-body rejection, and against a
/// candidate segment that happens to return some other kind of 200 (an HTML
/// error page, a JSON error object).
pub fn validate(canonical: &str, body: &str) -> Result<String, String> {
    let trimmed = strip_bom(body).trim();
    if trimmed.is_empty() {
        // This panel's own fork answers a blocked device / exhausted device
        // limit / non-Happ client with exactly this.
        return Err("empty body — device rejected by the remote panel \
                    (blocked, device limit reached, or HWID missing)"
            .to_string());
    }

    match canonical {
        "v2ray" => {
            // `decode_uri_list` now requires EVERY line to be a URI, so there
            // is no separate "are there any proxies in here" check left to
            // make — a body that gets this far is a URI list or it is nothing.
            decode_uri_list(trimmed)
                .ok_or_else(|| "body is neither a URI list nor base64 of one".to_string())
        }
        "v2ray-json" | "sing-box" => {
            let parsed: serde_json::Value = serde_json::from_str(trimmed)
                .map_err(|e| format!("body is not valid JSON: {e}"))?;
            if parsed.is_null() {
                return Err("body is JSON null".to_string());
            }
            Ok(trimmed.to_string())
        }
        "clash" | "clash-meta" => {
            // Not parsed as YAML on purpose — the payload is stored verbatim and
            // only ever concatenated, so a structural check is all that is
            // wanted, and adding a YAML dependency to check one key would not
            // pay for itself.
            if !trimmed.contains("proxies:") {
                return Err("clash body has no `proxies:` key".to_string());
            }
            Ok(trimmed.to_string())
        }
        other => Err(format!("unsupported format {other}")),
    }
}

/// A UTF-8 byte-order mark. `char::is_whitespace` returns false for it, so it
/// survives every `trim()` and would otherwise be read as the first character
/// of the first line's scheme — turning a perfectly good URI list into a
/// rejected one. Panels that emit it are rare but real.
const BOM: char = '\u{feff}';

fn strip_bom(s: &str) -> &str {
    s.strip_prefix(BOM).unwrap_or(s)
}

/// Whether one line is a proxy URI (`scheme://…`) rather than something that
/// merely contains those three characters somewhere.
///
/// The scheme must be at the START of the line. That is the whole distinction
/// between a URI list and an Xray JSON config, whose DNS block contains lines
/// like `"tcp://1.1.1.1",` and `"address": "https://…/dns-query",`.
fn is_uri_line(line: &str) -> bool {
    let Some(idx) = line.find("://") else {
        return false;
    };
    if idx == 0 {
        return false;
    }
    // `find` returns the byte offset of the match, which is always a char
    // boundary, so this slice cannot split a multi-byte character.
    let scheme = &line[..idx];
    scheme.starts_with(|c: char| c.is_ascii_alphabetic())
        && scheme
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
}

/// Whether a body really is a subscription URI list.
///
/// Every non-empty line must be a URI, with two allowances: blank lines, and
/// `#`-prefixed metadata lines (`#profile-title:` and friends, which some
/// panels in this family emit inline instead of as headers). A standalone `#`
/// line is metadata; a `#fragment` inside a URI is part of its own line.
///
/// STRICTNESS IS THE POINT. The previous rule — "the body contains `://`
/// somewhere" — accepted an Xray JSON config from a panel that dispatches
/// format on User-Agent, and 27 mangled JSON fragments were relabelled as
/// hosts and served to a live admin's users. Observed on example-f.com,
/// 2026-08-14. Anything that is not unmistakably a URI list must be rejected,
/// because the caller's fallback (retry with a neutral User-Agent, or keep the
/// last good payload) is always better than mirroring something we guessed at.
fn is_uri_list(text: &str) -> bool {
    let mut seen_uri = false;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if !is_uri_line(line) {
            return false;
        }
        seen_uri = true;
    }
    seen_uri
}

/// Return a URI list as plain text, decoding base64 if that is what arrived.
///
/// Both shapes are real: `/links` returns plain text, `/links_base64` and the
/// auto-dispatch endpoint return base64. Verified to produce byte-identical
/// output for both on the same subscription.
pub fn decode_uri_list(body: &str) -> Option<String> {
    let body = strip_bom(body);
    if is_uri_list(body) {
        return Some(body.to_string());
    }
    // Remote panels wrap base64 differently; strip all whitespace before
    // decoding, and tolerate missing padding.
    let compact: String = body
        .chars()
        .filter(|c| !c.is_whitespace() && *c != BOM)
        .collect();
    if compact.is_empty() {
        return None;
    }
    let padded = match compact.len() % 4 {
        0 => compact,
        n => format!("{compact}{}", "=".repeat(4 - n)),
    };
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(padded.as_bytes())
        .ok()?;
    let text = String::from_utf8(bytes).ok()?;
    if is_uri_list(&text) {
        Some(text)
    } else {
        None
    }
}

/// First line of a response body, truncated for a log/status message.
///
/// Truncates by CHARACTER, never by byte index. `&line[..max]` panics when byte
/// `max` lands inside a multi-byte character, and this runs on every non-200 —
/// including the ordinary dialect probe — with a body from a third party we do
/// not control. A localized nginx/WAF error page is enough to trigger it, and
/// because the release profile sets `panic = "abort"` a panic here takes the
/// whole process down rather than one task, into a restart loop that re-probes
/// the same URL. Found by review; the byte-slicing version shipped in befad2d.
fn first_line(s: &str, max: usize) -> String {
    let line = s.lines().next().unwrap_or("").trim();
    let mut out: String = line.chars().take(max).collect();
    if out.chars().count() < line.chars().count() {
        out.push('…');
    }
    out
}

/// The response body as text, refused once it passes `cap` bytes — before the
/// rest is buffered. A declared Content-Length over the cap is refused without
/// reading anything. Decoded as UTF-8 (lossy): every format mirrored here is
/// UTF-8 (base64 / URI lists, JSON, YAML).
async fn read_body_capped(mut resp: reqwest::Response, cap: usize) -> Result<String, String> {
    if resp.content_length().is_some_and(|len| len > cap as u64) {
        return Err(too_large(cap));
    }
    let mut body = Vec::new();
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| format!("cannot read body: {e}"))?
    {
        append_capped(&mut body, &chunk, cap)?;
    }
    Ok(String::from_utf8_lossy(&body).into_owned())
}

fn append_capped(body: &mut Vec<u8>, chunk: &[u8], cap: usize) -> Result<(), String> {
    if body.len() + chunk.len() > cap {
        return Err(too_large(cap));
    }
    body.extend_from_slice(chunk);
    Ok(())
}

fn too_large(cap: usize) -> String {
    format!("body larger than {} MB, refused", cap / (1024 * 1024))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A one-shot HTTP server on localhost that answers any request with
    /// `head` + `body`, then closes. Returns its URL.
    fn serve_once(head: &str, body: Vec<u8>) -> String {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        let mut response = head.as_bytes().to_vec();
        response.extend(body);
        std::thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let _ = sock.read(&mut [0u8; 4096]);
                // Errors once the client gives up on an oversized body.
                let _ = sock.write_all(&response);
            }
        });
        url
    }

    async fn read_from(head: &str, body: Vec<u8>) -> Result<String, String> {
        let resp = build_client()
            .get(serve_once(head, body))
            .send()
            .await
            .unwrap();
        read_body_capped(resp, MAX_BODY_BYTES).await
    }

    fn with_length(len: usize) -> String {
        format!("HTTP/1.1 200 OK\r\nContent-Length: {len}\r\nConnection: close\r\n\r\n")
    }

    // No Content-Length: the body runs until the server closes, so the cap
    // has to be enforced while reading, not from the header.
    const UNDECLARED: &str = "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n";

    #[tokio::test]
    async fn a_normal_body_is_read_whole() {
        let body = "vless://a@example.com:443#one\nvless://b@example.com:443#two".to_string();
        assert_eq!(
            read_from(&with_length(body.len()), body.clone().into_bytes()).await,
            Ok(body)
        );
    }

    #[tokio::test]
    async fn a_body_of_exactly_the_cap_is_accepted() {
        let body = vec![b'a'; MAX_BODY_BYTES];
        assert_eq!(
            read_from(UNDECLARED, body).await.map(|b| b.len()),
            Ok(MAX_BODY_BYTES)
        );
    }

    #[tokio::test]
    async fn a_declared_length_over_the_cap_is_refused_unread() {
        // Headers promise more than the cap, then the server sends nothing and
        // keeps the connection open. Only the Content-Length check can answer
        // that at once; a read would sit until the 20 s request timeout.
        let head = with_length(MAX_BODY_BYTES + 1);
        let started = std::time::Instant::now();
        let err = read_from_hanging(&head).await.unwrap_err();
        assert_eq!(err, "body larger than 2 MB, refused");
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "the body was waited for"
        );
    }

    /// Like `serve_once`, but sends only `head` and then holds the socket open.
    async fn read_from_hanging(head: &str) -> Result<String, String> {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        let head = head.as_bytes().to_vec();
        std::thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let _ = sock.read(&mut [0u8; 4096]);
                let _ = sock.write_all(&head);
                std::thread::sleep(Duration::from_secs(30));
            }
        });
        let resp = build_client().get(url).send().await.unwrap();
        read_body_capped(resp, MAX_BODY_BYTES).await
    }

    #[tokio::test]
    async fn an_undeclared_body_over_the_cap_is_refused_while_reading() {
        let body = vec![b'a'; MAX_BODY_BYTES + 1];
        assert_eq!(
            read_from(UNDECLARED, body).await.unwrap_err(),
            "body larger than 2 MB, refused"
        );
    }

    #[test]
    fn append_capped_stops_at_the_cap() {
        let mut body = Vec::new();
        assert!(append_capped(&mut body, b"12345", 8).is_ok());
        assert!(append_capped(&mut body, b"678", 8).is_ok()); // exactly 8
        assert!(append_capped(&mut body, b"9", 8).is_err());
        assert_eq!(body, b"12345678", "a refused chunk is not appended");
    }

    /// The exact shape that broke production: an Xray JSON config served by a
    /// panel that dispatches format on User-Agent. Its DNS block contains
    /// `://` on several lines, which is all the old check looked for.
    const XRAY_JSON: &str = r#"{
  "dns": {
    "servers": [
      {
        "address": "https://common.dot.dns.yandex.net/dns-query",
        "domains": ["geosite:category-ru"]
      },
      "tcp://1.1.1.1",
      "tcp://1.0.0.1"
    ]
  },
  "outbounds": [{"protocol": "vless", "tag": "proxy"}]
}"#;

    #[test]
    fn an_xray_json_config_is_not_a_uri_list() {
        // Regression: this was accepted, relabelled line by line, and served
        // to a live admin's users as 27 hosts (example-f.com, 2026-08-14).
        assert!(!is_uri_list(XRAY_JSON));
        assert!(decode_uri_list(XRAY_JSON).is_none());
        assert!(validate("v2ray", XRAY_JSON).is_err());
    }

    #[test]
    fn base64_of_an_xray_json_config_is_also_rejected() {
        let encoded = base64::engine::general_purpose::STANDARD.encode(XRAY_JSON);
        assert!(decode_uri_list(&encoded).is_none());
    }

    #[test]
    fn a_leading_byte_order_mark_does_not_reject_a_valid_list() {
        // char::is_whitespace('\u{feff}') is false, so no trim removes it.
        let body = "\u{feff}vless://uuid@host:443#NL\n";
        assert!(decode_uri_list(body).is_some());
        assert!(validate("v2ray", body).is_ok());
        // Also on the base64 path, where it would corrupt the alphabet.
        let encoded = format!(
            "\u{feff}{}",
            base64::engine::general_purpose::STANDARD.encode("vless://uuid@host:443#NL")
        );
        assert!(decode_uri_list(&encoded).is_some());
    }

    #[test]
    fn a_plain_uri_list_is_accepted() {
        let body = "vless://uuid@host:443?type=tcp#NL\ntrojan://pw@h2:443#DE\n";
        assert!(is_uri_list(body));
        assert_eq!(validate("v2ray", body).unwrap().trim(), body.trim());
    }

    #[test]
    fn base64_of_a_uri_list_is_accepted() {
        let plain = "vless://uuid@host:443#NL\nhysteria2://pw@h:443#FI";
        let encoded = base64::engine::general_purpose::STANDARD.encode(plain);
        assert_eq!(decode_uri_list(&encoded).as_deref(), Some(plain));
    }

    #[test]
    fn profile_title_decodes_the_panel_family_encoding() {
        // share.py encode_title() → "base64:" + b64(message)
        let encoded = format!(
            "base64:{}",
            base64::engine::general_purpose::STANDARD.encode("Abonement möhleti tamam")
        );
        assert_eq!(decode_profile_title(&encoded), "Abonement möhleti tamam");
        // Plain text and undecodable values pass through so an unknown
        // panel's title still reads in logs.
        assert_eq!(decode_profile_title("  Some Panel  "), "Some Panel");
        assert_eq!(decode_profile_title("base64:!!!"), "base64:!!!");
    }

    #[test]
    fn every_dead_status_title_is_recognised() {
        for title in REMOTE_DEAD_TITLES {
            assert!(is_remote_dead_title(title), "{title}");
            assert!(is_remote_dead_title(&format!("  {title} ")), "{title}");
        }
    }

    #[test]
    fn device_level_refusals_are_not_dead_accounts() {
        // These say OUR fetch identity was rejected — the account may be
        // alive, so its last-good payload must survive (TTL-bounded).
        for title in [
            "Blok Edildiniz",
            "Enjam çägi doly — admin bilen habarlaşyň",
            "Diňe Happ ulanyň",
            "Diňe Happ programmasy goldanýar",
            "",
        ] {
            assert!(!is_remote_dead_title(title), "{title:?}");
        }
    }

    #[test]
    fn inline_metadata_lines_do_not_disqualify_a_list() {
        // Some panels in this family put profile metadata in the body instead
        // of in headers. Those lines are not hosts, but they are not corruption
        // either — the relabeller already ignores anything without a scheme.
        let body = "#profile-title: base64:UGFuZWw=\n#profile-update-interval: 1\n\
                    vless://uuid@host:443#NL\n";
        assert!(is_uri_list(body));
    }

    #[test]
    fn a_body_of_only_metadata_has_no_hosts() {
        assert!(!is_uri_list("#profile-title: base64:UGFuZWw=\n"));
    }

    #[test]
    fn one_stray_non_uri_line_rejects_the_whole_body() {
        // Half-truncated responses and error pages appended to a list both
        // land here. Mirroring the good half would look like success.
        let body = "vless://uuid@host:443#NL\n<html>502 Bad Gateway</html>";
        assert!(!is_uri_list(body));
    }

    #[test]
    fn an_html_error_page_is_not_a_uri_list() {
        let body = "<html><body><a href=\"https://example.com\">login</a></body></html>";
        assert!(!is_uri_list(body));
        assert!(validate("v2ray", body).is_err());
    }

    #[test]
    fn a_scheme_must_start_the_line() {
        assert!(is_uri_line("vless://x"));
        assert!(is_uri_line("hysteria2://x"));
        // A quoted JSON value, a leading space, and a bare separator.
        assert!(!is_uri_line("\"tcp://1.1.1.1\","));
        assert!(!is_uri_line("://x"));
        assert!(!is_uri_line("address: https://x"));
        // Trailing junk after a valid scheme is still that host's business.
        assert!(is_uri_line("ss://YWVzOnB3@h:443#Label With Spaces"));
    }

    #[test]
    fn a_multibyte_line_does_not_panic() {
        // is_uri_line slices at the byte offset of "://"; a line whose prefix
        // is multi-byte must not split a character. panic = "abort" in release.
        assert!(!is_uri_line("«Россия» https://x"));
        assert!(is_uri_line("vless://uuid@host:443#🇷🇺 RU · vless"));
    }

    #[test]
    fn first_line_truncates_on_a_char_boundary_not_a_byte_index() {
        // The exact shape that panicked with byte slicing: 159 ASCII bytes then
        // a 2-byte character straddling byte 160.
        let body = "x".repeat(159) + &"ы".repeat(20);
        let out = first_line(&body, 160);
        assert!(out.ends_with('…'));
        assert_eq!(out.chars().count(), 161, "160 chars plus the ellipsis");
    }

    #[test]
    fn first_line_handles_multibyte_error_pages() {
        // A localized error page is the realistic trigger — a remote panel
        // behind an ISP/WAF interstitial in Russian, Turkmen or Persian.
        for body in [
            "Ошибка: доступ запрещён ".repeat(20),
            "Ýalňyşlyk: rugsat ýok ".repeat(20),
            "خطا: دسترسی ممنوع ".repeat(20),
            "日本語のエラーページ".repeat(30),
        ] {
            let out = first_line(&body, 160);
            assert!(out.chars().count() <= 161, "{out}");
        }
    }

    #[test]
    fn first_line_leaves_short_bodies_alone() {
        assert_eq!(first_line("HTTP 403 forbidden", 160), "HTTP 403 forbidden");
        assert_eq!(first_line("  padded  \nsecond line", 160), "padded");
        assert_eq!(first_line("", 160), "");
        // No ellipsis when the text is exactly at the limit.
        let exact = "y".repeat(160);
        assert_eq!(first_line(&exact, 160), exact);
    }

    #[test]
    fn v2ray_falls_back_to_the_bare_url_last() {
        let c = candidates_for("v2ray", None);
        assert_eq!(c.last().map(String::as_str), Some(BARE_URL));
        // Explicit segments are tried first: they do not depend on the remote
        // guessing a format from our User-Agent.
        assert_eq!(c[0], "links_base64");
    }

    #[test]
    fn only_v2ray_gets_the_bare_url_fallback() {
        // A bare URL returns a URI list; filing that under clash or xray would
        // store YAML/JSON-shaped keys containing something else entirely.
        for f in ["v2ray-json", "clash", "clash-meta", "sing-box"] {
            assert!(
                !candidates_for(f, None).iter().any(|s| s == BARE_URL),
                "{f} must not fall back to the bare URL"
            );
        }
    }

    #[test]
    fn bare_url_sentinel_cannot_collide_with_a_real_segment() {
        assert!(BARE_URL.starts_with('@'));
        for (_, segs) in FORMATS {
            for s in segs.iter().filter(|s| **s != BARE_URL) {
                assert!(s
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'));
            }
        }
    }

    #[test]
    fn a_payload_with_hash_directive_lines_still_validates() {
        // Verified live: a Happ subscription server returns directive lines
        // (#announce:, #profile-title:, #providerid) alongside the URIs, and the
        // #announce base64 decodes to the REMOTE panel's own username. They are
        // dropped downstream (relabel keeps only lines containing "://"), but
        // validation must not reject the payload for containing them.
        let body = "#announce: base64:8J+RpCBVbGFueWp5\n\
                    #subscription-pin: true\n\
                    vless://uuid@142.251.156.13:443?type=ws#Node\n\
                    #profile-title: base64:U3Vic2NyaXB0aW9u\n";
        let out = validate("v2ray", body).unwrap();
        assert!(out.contains("vless://"));
    }

    #[test]
    fn the_essential_format_is_the_one_the_panel_merges() {
        // Only this format's payload ever reaches a user, so only its failure is
        // an error. Keep it in sync with app/subscription/external.py's
        // MERGEABLE_FORMATS.
        assert_eq!(ESSENTIAL_FORMAT, "v2ray");
        assert!(FORMATS.iter().any(|(name, _)| *name == ESSENTIAL_FORMAT));
        // Fetched first, so its outcome is settled before the optional ones.
        assert_eq!(FORMATS[0].0, ESSENTIAL_FORMAT);
    }

    #[test]
    fn only_the_bare_url_gets_a_user_agent_retry() {
        // Explicit segments name the format in the path, so the UA is
        // irrelevant there and a second request would be pure waste.
        assert!(NEUTRAL_USER_AGENT != USER_AGENT);
        assert!(!NEUTRAL_USER_AGENT.starts_with("Happ/"));
    }

    #[test]
    fn v2ray_is_fetched_first() {
        // The only format the panel merges today; ordering keeps its failure
        // the first thing reported.
        assert_eq!(FORMATS[0].0, "v2ray");
    }

    #[test]
    fn candidates_put_the_remembered_segment_first_without_duplicating_it() {
        let c = candidates_for("v2ray", Some("v2ray"));
        assert_eq!(c, vec!["v2ray", "links_base64", "links", BARE_URL]);
        assert_eq!(c.iter().filter(|s| *s == "v2ray").count(), 1);
    }

    #[test]
    fn candidates_fall_back_to_the_canonical_list() {
        assert_eq!(
            candidates_for("v2ray", None),
            vec!["links_base64", "v2ray", "links", BARE_URL]
        );
        assert_eq!(
            candidates_for("v2ray", Some("")),
            candidates_for("v2ray", None)
        );
    }

    #[test]
    fn candidates_cover_both_observed_dialects() {
        // Left side: this panel's fork. Right side: the newer panel, verified
        // live from its own 422 enumeration.
        for (canonical, ours, theirs) in [
            ("v2ray-json", "v2ray-json", "xray"),
            ("clash-meta", "clash-meta", "clash_meta"),
            ("sing-box", "sing-box", "sing_box"),
        ] {
            let c = candidates_for(canonical, None);
            assert!(c.iter().any(|s| s == ours), "{canonical} missing {ours}");
            assert!(
                c.iter().any(|s| s == theirs),
                "{canonical} missing {theirs}"
            );
        }
    }

    #[test]
    fn unknown_format_has_no_candidates() {
        assert!(candidates_for("wireguard", None).is_empty());
    }

    #[test]
    fn empty_body_is_rejected_as_a_device_rejection() {
        // The fork's _announcement_response: HTTP 200, empty body, reason in a
        // header. Treating this as success would wipe good hosts.
        let err = validate("v2ray", "").unwrap_err();
        assert!(err.contains("empty body"), "{err}");
        assert!(validate("v2ray", "   \n  ").is_err());
    }

    #[test]
    fn plain_uri_list_validates_and_passes_through() {
        let body = "vless://uuid@1.2.3.4:443#A\nss://x@5.6.7.8:99#B";
        assert_eq!(validate("v2ray", body).unwrap(), body);
    }

    #[test]
    fn base64_uri_list_is_decoded() {
        let plain = "vless://uuid@1.2.3.4:443#A\nhysteria2://p@2.3.4.5:222#B";
        let encoded = base64::engine::general_purpose::STANDARD.encode(plain);
        assert_eq!(validate("v2ray", &encoded).unwrap(), plain);
    }

    #[test]
    fn base64_without_padding_still_decodes() {
        let plain = "vless://u@1.2.3.4:443#A";
        let encoded = base64::engine::general_purpose::STANDARD
            .encode(plain)
            .trim_end_matches('=')
            .to_string();
        assert_eq!(decode_uri_list(&encoded).as_deref(), Some(plain));
    }

    #[test]
    fn html_error_page_is_not_a_uri_list() {
        assert!(validate("v2ray", "<html><body>404</body></html>").is_err());
    }

    #[test]
    fn json_formats_require_parseable_json() {
        assert!(validate("v2ray-json", r#"{"outbounds":[]}"#).is_ok());
        assert!(validate("sing-box", r#"{"outbounds":[]}"#).is_ok());
        assert!(validate("sing-box", "not json").is_err());
        assert!(validate("sing-box", "null").is_err());
    }

    #[test]
    fn clash_formats_require_a_proxies_key() {
        assert!(validate("clash", "proxies:\n  - name: a\n").is_ok());
        assert!(validate("clash-meta", "port: 7890\nproxies:\n  - name: a\n").is_ok());
        assert!(validate("clash", "port: 7890\nrules: []\n").is_err());
    }

    #[test]
    fn unsupported_format_is_an_error_not_a_silent_pass() {
        assert!(validate("outline", "{}").is_err());
    }

    #[test]
    fn identity_strings_are_well_formed() {
        // The operator has to recognise these in the remote panel's device
        // list; that is the whole reason they are not random.
        assert!(USER_AGENT.starts_with("Happ/"));
        assert!(USER_AGENT.contains("poller"));
        // The fallback is a real HWID already registered as a device slot on the
        // remote users, so it matches an existing slot instead of claiming a new
        // one. It must stay a bare token: anything a header cannot carry
        // verbatim would be rejected or mangled by the remote panel.
        assert_eq!(FALLBACK_HWID.len(), 16);
        assert!(FALLBACK_HWID.chars().all(|c| c.is_ascii_alphanumeric()));
    }
}

//! External-subscription poller.
//!
//! Mirrors the hosts of subscriptions that live on OTHER panels into Redis, so
//! this panel can append them at subscription-render time for the admins each
//! subscription is assigned to (`external_subscription_admins`).
//!
//! ## Shape of one cycle
//!
//! ```text
//!   read enabled rows from external_subscriptions   (MySQL, READ-ONLY)
//!   for each row, bounded to MAX_CONCURRENT_SUBS at a time:
//!       for each format in fetch::FORMATS:
//!           GET <url>/<dialect segment>   with a Happ UA + the row's X-Hwid
//!           shape-check the response      (a 200 is not success — see fetch.rs)
//!           subscription-userinfo says the remote account is expired or out
//!               of quota?  →  DEL every payload key and stop: the fetch stays
//!               green for a dead account, the header is the only truth
//!           v2ray only: drop unroutable / .tm hosts, then REBUILD every
//!                       display name from country + scheme, copying no text
//!                       from the remote panel (see label.rs: label_line)
//!           SET extsub:payload:<id>:<format>          (24h TTL, see store.rs)
//!       SET extsub:status:<id>                        (no TTL)
//!   SET extsub:identity
//! ```
//!
//! ## Why this process never writes MySQL
//!
//! The registry is configuration; poll state is not. Keeping every write in
//! Redis means the panel DB has exactly one writer (the panel), and it means a
//! lost Redis heals on the next cycle instead of needing a repair path. The
//! trade is that a Redis flush costs one interval of external hosts, which is
//! why payloads are rewritten unconditionally every cycle (see `store.rs`).

pub mod fetch;
pub mod geo;
pub mod label;
pub mod store;
pub mod xray_json;

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use serde_json::json;
use sqlx::Row;
use tokio::sync::Semaphore;

/// How many subscriptions are fetched at once. Small on purpose: these are
/// someone else's panels, and a poller that hammers them is a poller that gets
/// rate-limited or blocked.
const MAX_CONCURRENT_SUBS: usize = 4;

/// How long a discovered path-segment dialect is trusted before being
/// re-probed.
const DIALECT_TTL_SECS: u64 = 24 * 3600;

/// One row of `external_subscriptions`.
#[derive(Debug, Clone)]
pub struct Subscription {
    pub id: u32,
    pub name: String,
    pub url: String,
    pub hwid: Option<String>,
}

/// Read the enabled registry rows. The only statement this service runs against
/// the panel database, and it is a read.
///
/// Rows are mapped by hand with `try_get` rather than a `FromRow` derive, to
/// match `collector.rs` and to keep sqlx's `macros` feature (and its proc-macro
/// build cost) out of the dependency set.
pub async fn load_subscriptions(pool: &sqlx::MySqlPool) -> Result<Vec<Subscription>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT id, name, url, hwid \
         FROM external_subscriptions \
         WHERE enabled = 1 \
         ORDER BY id",
    )
    .fetch_all(pool)
    .await?;

    let mut subs = Vec::with_capacity(rows.len());
    for row in rows {
        subs.push(Subscription {
            id: row.try_get::<i32, _>("id")? as u32,
            name: row.try_get("name")?,
            url: row.try_get("url")?,
            hwid: row.try_get("hwid")?,
        });
    }
    Ok(subs)
}

/// Run one full cycle. Never returns an error for a single subscription's
/// failure — each row's outcome is recorded in its own status key.
pub async fn run_cycle(
    pool: &sqlx::MySqlPool,
    redis_url: &str,
    http: &reqwest::Client,
) -> Result<usize, String> {
    let subs = load_subscriptions(pool)
        .await
        .map_err(|e| format!("cannot read external_subscriptions: {e}"))?;

    // Published before the empty-registry check on purpose: this is the
    // fallback HWID the operator reads in the panel to decide what to pre-fill
    // on a remote user, which is needed BEFORE the first subscription exists.
    match store::connect(redis_url).await {
        Some(mut conn) => publish_identity(&mut conn).await,
        None => return Err("Redis unavailable; skipping cycle".to_string()),
    }

    if subs.is_empty() {
        // Info, not debug: an idle poller must be distinguishable from a wedged
        // one in the container log without reaching for docker inspect.
        tracing::info!("no enabled external subscriptions; nothing to mirror");
        return Ok(0);
    }

    let sem = Arc::new(Semaphore::new(MAX_CONCURRENT_SUBS));
    let mut handles = Vec::with_capacity(subs.len());

    for sub in subs {
        let permit_source = sem.clone();
        let http = http.clone();
        let redis_url = redis_url.to_string();
        handles.push(tokio::spawn(async move {
            let _permit = match permit_source.acquire().await {
                Ok(p) => p,
                Err(_) => return,
            };
            // A connection per subscription rather than a shared one: a stalled
            // multiplexed connection would otherwise serialise every row behind
            // it, and these are at most MAX_CONCURRENT_SUBS at a time.
            let mut conn = match store::connect(&redis_url).await {
                Some(c) => c,
                None => return,
            };
            poll_one(&http, &mut conn, &sub).await;
        }));
    }

    let total = handles.len();
    for h in handles {
        // NB: this only reports a panic in debug/test builds. The release
        // profile sets `panic = "abort"`, so a panic in a spawned task takes the
        // whole process down before this JoinError can be observed — which is
        // why the code these tasks run must not panic in the first place (see
        // fetch::first_line) rather than relying on task isolation.
        if let Err(e) = h.await {
            tracing::error!("subscription task panicked: {e}");
        }
    }
    Ok(total)
}

async fn publish_identity(conn: &mut store::Conn) {
    let payload = json!({
        "hwid": fetch::FALLBACK_HWID,
        "user_agent": fetch::USER_AGENT,
    })
    .to_string();
    store::set(conn, "extsub:identity", &payload).await;
}

/// Fetch every format for one subscription and write its payloads and status.
async fn poll_one(http: &reqwest::Client, conn: &mut store::Conn, sub: &Subscription) {
    let hwid = sub
        .hwid
        .as_deref()
        .map(str::trim)
        .filter(|h| !h.is_empty())
        .unwrap_or(fetch::FALLBACK_HWID);
    let using_fallback = sub.hwid.as_deref().map(str::trim).unwrap_or("").is_empty();

    let mut host_counts: HashMap<String, usize> = HashMap::new();
    let mut countries: BTreeSet<String> = BTreeSet::new();
    let mut mismatches: Vec<String> = Vec::new();
    let mut corrections: Vec<String> = Vec::new();
    let mut excluded: Vec<String> = Vec::new();
    let mut fronted = 0usize;
    let mut userinfo: Option<String> = None;
    let mut dead_reason: Option<String> = None;
    let mut first_error: Option<(Option<u16>, String)> = None;
    let mut unavailable: Vec<String> = Vec::new();
    let mut essential_ok = false;
    let mut json_payload: Option<String> = None;
    let mut derived_from_json = false;
    let mut json_skipped: Vec<String> = Vec::new();

    for (canonical, _) in fetch::FORMATS {
        let dialect_key = store::dialect_key(sub.id, canonical);
        let cached_segment = store::get(conn, &dialect_key).await;

        let fetched =
            match fetch::fetch_format(http, &sub.url, hwid, canonical, cached_segment.as_deref())
                .await
            {
                Ok(f) => f,
                Err(e) => {
                    // An announcement whose title matches the panel family's
                    // non-active status vocabulary means the ACCOUNT behind
                    // this subscription is dead — same consequence as the
                    // userinfo check below, reached instead of it once the
                    // remote runs the status gate (a lapsed user then answers
                    // with an empty 200 + title and no userinfo header). Any
                    // other rejection stays a plain failure: last-good data
                    // holds, bounded by the payload TTL.
                    //
                    // DELIBERATE: this fires even when an earlier format
                    // already published this cycle (the account died between
                    // two of our sequential requests — the remote reviews
                    // statuses every 10s). The later response is the newer
                    // truth, so the post-loop delete wins; a recovery
                    // republishes on the next cycle.
                    if let Some(title) = e
                        .rejection_title
                        .as_deref()
                        .filter(|t| fetch::is_remote_dead_title(t))
                    {
                        dead_reason =
                            Some(format!("remote panel refuses this subscription: {title}"));
                        break;
                    }
                    // Only the essential format's failure is an error. A remote
                    // that simply does not offer clash or sing-box is normal and
                    // must not show up as a broken subscription.
                    if *canonical == fetch::ESSENTIAL_FORMAT {
                        tracing::warn!(
                            "sub {} ({}) format {} FAILED: {}",
                            sub.id,
                            sub.name,
                            canonical,
                            e.message
                        );
                        first_error = Some((e.http_status, e.message));
                    } else {
                        tracing::debug!(
                            "sub {} ({}) does not offer {}: {}",
                            sub.id,
                            sub.name,
                            canonical,
                            e.message
                        );
                        unavailable.push((*canonical).to_string());
                    }
                    continue;
                }
            };

        store::set_ex(conn, &dialect_key, &fetched.segment, DIALECT_TTL_SECS).await;
        if userinfo.is_none() {
            userinfo = fetched.userinfo.clone();
            // The remote panel serves a subscription regardless of the
            // account's status, so an expired or quota-exhausted integration
            // account still fetches "successfully" — this header is the only
            // place the remote states it. Checked before anything is
            // published: those hosts are already cut off on the remote, and
            // mirroring them would hand every eligible user a config full of
            // dead endpoints.
            if let Some(raw) = userinfo.as_deref() {
                dead_reason =
                    remote_account_dead(&parse_userinfo(raw), chrono::Utc::now().timestamp());
                if dead_reason.is_some() {
                    break;
                }
            }
        }

        // Country verification only applies to the URI-list format: that is
        // where the label lives in a `#fragment` this poller can safely rewrite.
        let payload = if *canonical == "v2ray" {
            let outcome = relabel(http, conn, &fetched.payload).await;
            host_counts.insert((*canonical).to_string(), outcome.hosts);
            countries.extend(outcome.countries);
            mismatches.extend(outcome.mismatches);
            corrections.extend(outcome.corrections);
            excluded.extend(outcome.excluded);
            fronted += outcome.fronted;
            outcome.payload
        } else {
            fetched.payload
        };

        if *canonical == "v2ray-json" {
            json_payload = Some(payload.clone());
        }

        if store::set_ex(
            conn,
            &store::payload_key(sub.id, canonical),
            &payload,
            store::PAYLOAD_TTL_SECS,
        )
        .await
            && *canonical == fetch::ESSENTIAL_FORMAT
        {
            essential_ok = true;
        }
    }

    // Last resort: some panels serve ONLY Xray JSON. Measured live against
    // example-f.com — every URI-list route answers 406/500/422 and only
    // /xray returns anything — so without this the subscription can never be
    // mirrored at all, because the panel merges the URI list and nothing else,
    // and this fork's Happ users receive that list rather than JSON.
    //
    // Converting produces ordinary URI lines, so everything downstream (the
    // unroutable/.tm filter, geo verification, the label rebuild that keeps
    // remote remarks out of our users' configs) applies exactly as it does to
    // a panel that served a list in the first place.
    if !essential_ok && dead_reason.is_none() {
        if let Some(json) = json_payload.as_deref() {
            match xray_json::to_uri_lines(json) {
                Ok(converted) if !converted.lines.is_empty() => {
                    tracing::info!(
                        "sub {} ({}): no URI-list route on this panel; derived {} host(s) from \
                         its Xray JSON{}",
                        sub.id,
                        sub.name,
                        converted.lines.len(),
                        if converted.skipped.is_empty() {
                            String::new()
                        } else {
                            format!(" ({} skipped)", converted.skipped.len())
                        }
                    );
                    derived_from_json = true;
                    json_skipped = converted.skipped;

                    let outcome = relabel(http, conn, &converted.lines.join("\n")).await;
                    host_counts.insert("v2ray".to_string(), outcome.hosts);
                    countries.extend(outcome.countries);
                    mismatches.extend(outcome.mismatches);
                    corrections.extend(outcome.corrections);
                    excluded.extend(outcome.excluded);
                    fronted += outcome.fronted;

                    if outcome.hosts > 0
                        && store::set_ex(
                            conn,
                            &store::payload_key(sub.id, fetch::ESSENTIAL_FORMAT),
                            &outcome.payload,
                            store::PAYLOAD_TTL_SECS,
                        )
                        .await
                    {
                        essential_ok = true;
                        // The URI-list fetch "failing" is the normal state for
                        // this kind of panel, not a fault worth showing red.
                        first_error = None;
                    }
                }
                Ok(converted) => {
                    json_skipped = converted.skipped;
                    tracing::warn!(
                        "sub {} ({}): its Xray JSON yielded no usable hosts",
                        sub.id,
                        sub.name
                    );
                }
                Err(e) => tracing::warn!(
                    "sub {} ({}): cannot derive hosts from its Xray JSON: {e}",
                    sub.id,
                    sub.name
                ),
            }
        }
    }

    // A dead remote account is a confirmed state, not a fetch failure: the
    // remote's own panel said the account is expired or out of quota, so its
    // hosts are already cut off there. Delete every payload now instead of
    // leaving last-good data to serve dead endpoints until the TTL. Recovery
    // is automatic — the header is re-read every cycle, and the first healthy
    // one republishes.
    if let Some(reason) = dead_reason.as_deref() {
        tracing::warn!(
            "sub {} ({}): {}; removing mirrored hosts until the remote account recovers",
            sub.id,
            sub.name,
            reason
        );
        let keys: Vec<String> = fetch::FORMATS
            .iter()
            .map(|(canonical, _)| store::payload_key(sub.id, canonical))
            .collect();
        store::del(conn, &keys).await;
        // The status must not advertise counts for payloads this block just
        // deleted — an "ok: false" status showing N hosts reads as "N hosts
        // still being served".
        host_counts.clear();
        countries.clear();
    }

    // "ok" means the users served by this subscription got their hosts, not
    // that some format or other happened to fetch.
    let ok = essential_ok && dead_reason.is_none();
    if !ok {
        if dead_reason.is_none() {
            tracing::error!(
                "sub {} ({}) produced no usable {} payload this cycle; last good data left in \
                 place until the payload TTL",
                sub.id,
                sub.name,
                fetch::ESSENTIAL_FORMAT
            );
            // Keys written before the TTL rule (or by an older poller) carry
            // no expiry, and a failing subscription is exactly the one whose
            // payloads never get rewritten with one. NX: a failed cycle may
            // add the missing bound, never extend an existing one. One
            // pipelined round trip — this branch repeats every cycle for as
            // long as the remote stays broken.
            let keys: Vec<String> = fetch::FORMATS
                .iter()
                .map(|(canonical, _)| store::payload_key(sub.id, canonical))
                .collect();
            store::expire_nx(conn, &keys, store::PAYLOAD_TTL_SECS).await;
        }
    } else if !mismatches.is_empty() {
        tracing::info!(
            "sub {} ({}): {} host(s) claim a country their IP disagrees with: {}",
            sub.id,
            sub.name,
            mismatches.len(),
            mismatches.join(", ")
        );
    }

    // A dead account outranks any fetch error in the status: the fetches may
    // even have succeeded, and "the remote account expired" is the actionable
    // message, not whatever a secondary format returned. `http_status` first —
    // it needs `dead_reason` only as a flag, so `error_msg` can then take both
    // Strings by move.
    let http_status = if dead_reason.is_some() {
        None
    } else {
        first_error.as_ref().and_then(|(s, _)| *s)
    };
    let error_msg = dead_reason.or_else(|| first_error.map(|(_, m)| m));

    let status = json!({
        "ok": ok,
        "fetched_at": chrono::Utc::now().to_rfc3339(),
        "http_status": http_status,
        "error": error_msg,
        "host_counts": host_counts,
        "countries": countries.iter().cloned().collect::<Vec<_>>(),
        "remote_userinfo": userinfo,
        // Diagnostics the panel does not model but an operator wants when a
        // label looks wrong. Extra keys are ignored by the Pydantic response.
        "label_mismatches": mismatches,
        "label_corrections": corrections,
        // Informational, not a failure: formats this remote does not serve.
        "unavailable_formats": unavailable,
        "excluded_hosts": excluded,
        // True when this row has no URI-list route at all and its hosts were
        // rebuilt from the panel's Xray JSON instead.
        "derived_from_json": derived_from_json,
        "json_skipped": json_skipped,
        "fronted_hosts": fronted,
        "using_fallback_hwid": using_fallback,
    })
    .to_string();
    store::set(conn, &store::status_key(sub.id), &status).await;
}

/// The remote user's own accounting, from the `subscription-userinfo` header:
/// `upload=..; download=..; total=..; expire=..`. Zero means "unlimited" for
/// `total` and "never" for `expire` — the same semantics this panel's own
/// subscription route emits. A missing or unparseable field is `None`, never
/// zero: an absent value is unknown, and unknown must not read as unlimited
/// (nor, worse, as exhausted).
#[derive(Debug, Default, PartialEq, Eq)]
struct RemoteUserinfo {
    upload: Option<u64>,
    download: Option<u64>,
    total: Option<u64>,
    expire: Option<i64>,
}

fn parse_userinfo(raw: &str) -> RemoteUserinfo {
    let mut info = RemoteUserinfo::default();
    for pair in raw.split(';') {
        let Some((key, value)) = pair.split_once('=') else {
            continue;
        };
        let value = value.trim();
        match key.trim().to_ascii_lowercase().as_str() {
            "upload" => info.upload = value.parse().ok(),
            "download" => info.download = value.parse().ok(),
            "total" => info.total = value.parse().ok(),
            "expire" => info.expire = value.parse().ok(),
            _ => {}
        }
    }
    info
}

/// Whether the remote account behind a subscription is dead — expired, or out
/// of quota — per its own `subscription-userinfo`. Returns the operator-facing
/// reason, or `None` while the account is (as far as the header says) alive.
///
/// This is the check the whole feature's failure model hinges on: a remote
/// panel in this family serves the subscription for an expired or capped
/// account exactly as it does for a healthy one, so no amount of response
/// validation can catch it. Both comparisons are exact, not heuristic —
/// `used >= total` and `expire < now` are the same conditions the remote's own
/// `review_users` job enforces when it cuts the account off its nodes.
fn remote_account_dead(info: &RemoteUserinfo, now_unix: i64) -> Option<String> {
    if let Some(expire) = info.expire {
        if expire > 0 && expire < now_unix {
            let when = chrono::DateTime::from_timestamp(expire, 0)
                .map(|d| d.to_rfc3339())
                .unwrap_or_else(|| expire.to_string());
            return Some(format!("remote account expired at {when}"));
        }
    }
    if let Some(total) = info.total {
        let used = info
            .upload
            .unwrap_or(0)
            .saturating_add(info.download.unwrap_or(0));
        if total > 0 && used >= total {
            return Some(format!(
                "remote account exhausted its quota ({used} of {total} bytes used)"
            ));
        }
    }
    None
}

struct Relabelled {
    payload: String,
    hosts: usize,
    countries: Vec<String>,
    /// Hosts whose claimed country disagrees with their IP and was KEPT, because
    /// the IP is a CDN edge or a multi-country relay.
    mismatches: Vec<String>,
    /// Hosts whose label was rewritten to the verified country.
    corrections: Vec<String>,
    /// Hosts dropped from the payload entirely, with the reason.
    excluded: Vec<String>,
    fronted: usize,
}

/// Verify every host's country and fill in labels that claim none.
///
/// Two independent fronting signals are honoured, because either one alone
/// misses cases: the provider's identity from ip-api (a CDN ASN), and one IP
/// carrying several *different* claimed countries inside the same payload —
/// which is what a port-multiplexed relay looks like, and no external service
/// can tell you that.
async fn relabel(http: &reqwest::Client, conn: &mut store::Conn, payload: &str) -> Relabelled {
    let all: Vec<&str> = payload.lines().filter(|l| l.contains("://")).collect();

    // Drop hosts that must never reach a user's config before anything else
    // looks at them — an excluded host should not consume a geo lookup, should
    // not influence the multi-country relay heuristic, and above all should not
    // survive into the stored payload. A line whose host cannot be read (a
    // vmess base64 blob) is kept: refusing to mirror what we cannot inspect
    // would silently drop working hosts.
    let mut lines: Vec<&str> = Vec::with_capacity(all.len());
    let mut excluded: Vec<String> = Vec::new();
    for line in all {
        // A scheme we cannot rebuild the display name for would carry the remote
        // operator's remark through verbatim, so it is dropped, not mirrored.
        if let Some(reason) = label::uninspectable_scheme(line) {
            excluded.push(format!("{} ({reason})", label::scheme_of(line)));
            continue;
        }
        match label::host_of(line).and_then(|h| label::excluded_reason(&h).map(|r| (h, r))) {
            Some((host, reason)) => excluded.push(format!("{host} ({reason})")),
            None => lines.push(line),
        }
    }
    if !excluded.is_empty() {
        tracing::info!(
            "excluded {} host(s): {}",
            excluded.len(),
            excluded.join(", ")
        );
    }

    // First pass: hosts, their claimed countries, and their geo.
    let mut claims_per_host: HashMap<String, BTreeSet<String>> = HashMap::new();
    let mut geo_per_host: HashMap<String, geo::Geo> = HashMap::new();
    for line in &lines {
        let host = match label::host_of(line) {
            Some(h) => h,
            None => continue,
        };
        if let Some(parts) = label::split_line(line) {
            let name = label::percent_decode(parts.name_enc);
            if let Some(cc) = label::claimed_cc(&name) {
                claims_per_host.entry(host.clone()).or_default().insert(cc);
            }
        }
        if !geo_per_host.contains_key(&host) {
            geo_per_host.insert(host.clone(), geo::lookup(http, conn, &host).await);
        }
    }

    let mut out_lines = Vec::with_capacity(lines.len());
    let mut countries = BTreeSet::new();
    let mut mismatches = Vec::new();
    let mut corrections = Vec::new();
    let mut fronted_hosts: BTreeSet<String> = BTreeSet::new();
    let mut used_names: HashMap<String, usize> = HashMap::new();

    for line in &lines {
        let host = label::host_of(line);
        let (verified_cc, is_fronted) = match host.as_ref().and_then(|h| geo_per_host.get(h)) {
            Some(g) => {
                let relay = host
                    .as_ref()
                    .and_then(|h| claims_per_host.get(h))
                    .map(|s| s.len() > 1)
                    .unwrap_or(false);
                (g.cc.clone(), g.fronted || relay)
            }
            None => (None, false),
        };
        if is_fronted {
            if let Some(h) = host.clone() {
                fronted_hosts.insert(h);
            }
        }
        if let Some(cc) = verified_cc.as_deref() {
            countries.insert(cc.to_ascii_uppercase());
        }

        let claimed = label::split_line(line)
            .map(|p| label::percent_decode(p.name_enc))
            .and_then(|n| label::claimed_cc(&n));

        let (new_line, outcome) = label::label_line(line, verified_cc.as_deref(), is_fronted);

        // Both a correction and a refusal-to-correct are worth surfacing: the
        // first says a country was wrong and is now right, the second says a
        // country disagrees with its IP and was deliberately trusted anyway.
        let host_name = host.clone().unwrap_or_else(|| "?".into());
        let verified = verified_cc.clone().unwrap_or_else(|| "?".into());
        let claim = claimed.clone().unwrap_or_default();
        match outcome {
            label::LabelOutcome::Corrected => {
                corrections.push(format!("{host_name}: {claim} -> {verified}"))
            }
            label::LabelOutcome::KeptFronted => mismatches.push(format!(
                "{host_name} claims {claim} but resolves to {verified} (fronted; kept)"
            )),
            _ => {}
        }

        // Names are rebuilt from country + scheme only, so two hosts in one
        // country on the same protocol collide. Number the repeats in payload
        // order — without this a user sees several identical entries and cannot
        // tell which is which.
        let mut candidate = new_line;
        if let Some(parts) = label::split_line(&candidate) {
            let base = label::percent_decode(parts.name_enc);
            let seen = used_names.entry(base.clone()).or_insert(0);
            *seen += 1;
            if *seen > 1 {
                let numbered = format!("{base} {}", *seen);
                candidate = parts.with_name(&label::encode_name(&numbered));
            }
        }
        out_lines.push(candidate);
    }

    Relabelled {
        payload: out_lines.join("\n"),
        hosts: out_lines.len(),
        countries: countries.into_iter().collect(),
        mismatches,
        corrections,
        excluded,
        fronted: fronted_hosts.len(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn userinfo_parses_the_header_this_fork_emits() {
        let info = parse_userinfo("upload=123; download=456; total=1000; expire=1767225600");
        assert_eq!(
            info,
            RemoteUserinfo {
                upload: Some(123),
                download: Some(456),
                total: Some(1000),
                expire: Some(1767225600),
            }
        );
    }

    #[test]
    fn userinfo_tolerates_reordering_case_and_unknown_keys() {
        let info = parse_userinfo("Expire=10;TOTAL=20 ; junk ; foo=bar; download=5;upload=1");
        assert_eq!(info.upload, Some(1));
        assert_eq!(info.download, Some(5));
        assert_eq!(info.total, Some(20));
        assert_eq!(info.expire, Some(10));
    }

    #[test]
    fn an_empty_or_garbage_value_is_unknown_not_zero() {
        // `Number("") === 0` is exactly the bug the dashboard's own userinfo
        // parser has; the poller must not repeat it — `total=` reading as 0
        // would flip the meaning from "unknown" to "unlimited".
        let info = parse_userinfo("upload=; download=x; total=; expire=soon");
        assert_eq!(info, RemoteUserinfo::default());
    }

    #[test]
    fn negative_one_convention_reads_as_unknown_not_zero() {
        // Some panels send -1 for "unlimited". u64 parsing rejects the sign
        // outright, so these read as None (unknown) — never 0, never a
        // wrapped huge number — and unknown can never classify as exhausted.
        let info = parse_userinfo("upload=-1; download=-1; total=-1; expire=-1");
        assert_eq!(info.upload, None);
        assert_eq!(info.download, None);
        assert_eq!(info.total, None);
        // expire is i64, so -1 parses — and is not > 0, so it means "never".
        assert_eq!(info.expire, Some(-1));
        assert_eq!(remote_account_dead(&info, 1_000), None);
    }

    #[test]
    fn a_healthy_account_is_not_dead() {
        let info = parse_userinfo("upload=10; download=10; total=100; expire=0");
        assert_eq!(remote_account_dead(&info, 1_000), None);
    }

    #[test]
    fn zero_total_and_zero_expire_mean_unlimited() {
        let info = parse_userinfo("upload=999; download=999; total=0; expire=0");
        assert_eq!(remote_account_dead(&info, i64::MAX - 1), None);
    }

    #[test]
    fn quota_exhaustion_is_dead_at_exactly_the_limit() {
        let info = parse_userinfo("upload=40; download=60; total=100; expire=0");
        let reason = remote_account_dead(&info, 1_000).expect("exhausted");
        assert!(reason.contains("quota"), "{reason}");
        assert!(reason.contains("100 of 100"), "{reason}");
    }

    #[test]
    fn one_byte_under_the_limit_is_alive() {
        let info = parse_userinfo("upload=40; download=59; total=100; expire=0");
        assert_eq!(remote_account_dead(&info, 1_000), None);
    }

    #[test]
    fn download_alone_can_prove_exhaustion() {
        // A fork that omits `upload` must still be caught once `download`
        // alone crosses the cap.
        let info = parse_userinfo("download=100; total=100");
        assert!(remote_account_dead(&info, 1_000).is_some());
    }

    #[test]
    fn a_missing_total_can_never_be_exhausted() {
        let info = parse_userinfo("upload=999; download=999");
        assert_eq!(remote_account_dead(&info, 1_000), None);
    }

    #[test]
    fn used_bytes_saturate_instead_of_overflowing() {
        let info = RemoteUserinfo {
            upload: Some(u64::MAX),
            download: Some(u64::MAX),
            total: Some(1),
            expire: None,
        };
        assert!(remote_account_dead(&info, 1_000).is_some());
    }

    #[test]
    fn an_expired_account_is_dead_and_names_the_moment() {
        let info = parse_userinfo("upload=0; download=0; total=0; expire=1000");
        let reason = remote_account_dead(&info, 2000).expect("expired");
        assert!(reason.contains("expired"), "{reason}");
        // rfc3339 of t=1000, so the operator sees a date, not an epoch.
        assert!(reason.contains("1970-01-01"), "{reason}");
    }

    #[test]
    fn a_future_expiry_is_alive() {
        let info = parse_userinfo("expire=2000");
        assert_eq!(remote_account_dead(&info, 1000), None);
    }

    #[test]
    fn expiry_outranks_quota_in_the_reason() {
        // Both conditions hold; the reported reason must be deterministic so
        // the status text does not flap between cycles.
        let info = parse_userinfo("upload=100; download=0; total=100; expire=1000");
        let reason = remote_account_dead(&info, 2000).expect("dead");
        assert!(reason.contains("expired"), "{reason}");
    }

    #[test]
    fn no_userinfo_fields_at_all_is_alive() {
        assert_eq!(remote_account_dead(&RemoteUserinfo::default(), 1_000), None);
    }
}

//! IP → country, via ip-api.com, cached in Redis.
//!
//! Same provider and request shape as the user's vpn-watchdog
//! (`src/utils.rs::resolve_country_from_ip`), so both projects agree on what a
//! host's country is. Extended with `isp`/`as` because those are what identify
//! a CDN edge — and a CDN edge's country says nothing about where the traffic
//! actually exits (see `label.rs` for the measured evidence).
//!
//! Best-effort throughout: any failure yields `None` and the caller leaves the
//! host's label alone. A wrong label is worse than no label.
//!
//! Caching matters more than it looks. The free tier is rate-limited (45
//! req/min) and a payload can hold dozens of hosts, so an uncached poller would
//! start getting throttled as soon as a few subscriptions are registered. Hosts
//! change rarely, hence a 7-day TTL; the cache is also the reason a normal
//! cycle makes no geo requests at all.

use std::net::IpAddr;
use std::time::Duration;

use serde_json::Value;

use super::store;

/// 7 days. Hosts are long-lived; the whole point is that a steady-state cycle
/// costs zero geo lookups.
const GEO_TTL_SECS: u64 = 7 * 24 * 3600;

/// Substrings that identify a CDN / fronting provider in ip-api's `isp` or
/// `as` field. Matched case-insensitively. A host behind one of these is never
/// relabelled: the IP is an edge PoP, not the exit.
const FRONTING_MARKERS: &[&str] = &[
    "cloudflare",
    "akamai",
    "fastly",
    "cloudfront",
    "amazon",
    "google",
    "microsoft",
    "azure",
    "gcore",
    "bunny",
    "ddos-guard",
    "qrator",
];

#[derive(Debug, Clone, PartialEq)]
pub struct Geo {
    pub country: Option<String>,
    pub cc: Option<String>,
    pub fronted: bool,
}

impl Geo {
    fn empty() -> Self {
        Geo {
            country: None,
            cc: None,
            fronted: false,
        }
    }
}

/// Resolve a host (IP literal or domain) to its country, cached.
///
/// A domain is resolved to an address first, which is exactly where a fronted
/// host stops being informative — `node.example.com` behind Cloudflare resolves
/// to a Cloudflare edge. That is detected, not worked around.
pub async fn lookup(
    http: &reqwest::Client,
    redis: &mut redis::aio::MultiplexedConnection,
    host: &str,
) -> Geo {
    let ip = match resolve_to_ip(host).await {
        Some(ip) => ip,
        None => return Geo::empty(),
    };

    let key = format!("extsub:geo:{ip}");
    if let Some(raw) = store::get(redis, &key).await {
        if let Some(geo) = parse_cached(&raw) {
            return geo;
        }
    }

    let geo = query_ip_api(http, &ip).await;
    // Cache negative results too: a lookup that failed for a reason other than
    // rate-limiting (reserved range, unroutable address) would otherwise be
    // retried on every single cycle.
    if let Ok(encoded) = serde_json::to_string(&serde_json::json!({
        "country": geo.country,
        "cc": geo.cc,
        "fronted": geo.fronted,
    })) {
        store::set_ex(redis, &key, &encoded, GEO_TTL_SECS).await;
    }
    geo
}

/// A host string as an IP, resolving via DNS when it is a domain.
async fn resolve_to_ip(host: &str) -> Option<String> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Some(ip.to_string());
    }
    // Port is irrelevant here but `lookup_host` requires one.
    let target = format!("{host}:443");
    match tokio::time::timeout(Duration::from_secs(5), tokio::net::lookup_host(target)).await {
        Ok(Ok(mut addrs)) => addrs.next().map(|a| a.ip().to_string()),
        _ => None,
    }
}

fn parse_cached(raw: &str) -> Option<Geo> {
    let v: Value = serde_json::from_str(raw).ok()?;
    Some(Geo {
        country: v["country"].as_str().map(String::from),
        cc: v["cc"].as_str().map(String::from),
        fronted: v["fronted"].as_bool().unwrap_or(false),
    })
}

async fn query_ip_api(http: &reqwest::Client, ip: &str) -> Geo {
    let url = format!("http://ip-api.com/json/{ip}?fields=status,country,countryCode,isp,as");
    let resp = match http.get(&url).send().await {
        Ok(r) => r,
        Err(e) => {
            tracing::debug!("geo lookup for {ip} failed: {e}");
            return Geo::empty();
        }
    };
    let json: Value = match resp.json().await {
        Ok(v) => v,
        Err(e) => {
            tracing::debug!("geo lookup for {ip} returned unparseable JSON: {e}");
            return Geo::empty();
        }
    };
    if json["status"].as_str() != Some("success") {
        tracing::debug!("geo lookup for {ip} was not successful: {json}");
        return Geo::empty();
    }

    let isp = json["isp"].as_str().unwrap_or("");
    let asn = json["as"].as_str().unwrap_or("");
    Geo {
        country: json["country"].as_str().map(String::from),
        cc: json["countryCode"].as_str().map(String::from),
        fronted: is_fronting(isp) || is_fronting(asn),
    }
}

/// Does this ISP / AS description belong to a CDN or fronting provider?
pub fn is_fronting(desc: &str) -> bool {
    let lower = desc.to_ascii_lowercase();
    FRONTING_MARKERS.iter().any(|m| lower.contains(m))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_cdn_providers_from_live_field_values() {
        // Verbatim from ip-api for 198.18.44.0, a host in a real payload.
        assert!(is_fronting("Cloudflare, Inc."));
        assert!(is_fronting("AS13335 Cloudflare, Inc."));
        assert!(is_fronting("Amazon CloudFront"));
        assert!(is_fronting("FASTLY"));
    }

    #[test]
    fn leaves_ordinary_hosting_providers_alone() {
        // Verbatim from ip-api for the two real exit hosts.
        assert!(!is_fronting("Hetzner Online GmbH"));
        assert!(!is_fronting("AS24940 Hetzner Online GmbH"));
        assert!(!is_fronting("Enterprise Cloud Ltd."));
        assert!(!is_fronting(""));
    }

    #[test]
    fn cached_blob_round_trips() {
        let raw = r#"{"country":"Germany","cc":"DE","fronted":false}"#;
        let geo = parse_cached(raw).unwrap();
        assert_eq!(geo.cc.as_deref(), Some("DE"));
        assert_eq!(geo.country.as_deref(), Some("Germany"));
        assert!(!geo.fronted);
    }

    #[test]
    fn cached_negative_blob_is_valid() {
        let geo = parse_cached(r#"{"country":null,"cc":null,"fronted":false}"#).unwrap();
        assert_eq!(geo, Geo::empty());
    }

    #[test]
    fn garbage_cache_entry_is_rejected_not_trusted() {
        assert!(parse_cached("not json").is_none());
    }

    #[tokio::test]
    async fn ip_literals_need_no_dns() {
        assert_eq!(
            resolve_to_ip("198.18.43.215").await.as_deref(),
            Some("198.18.43.215")
        );
        assert_eq!(resolve_to_ip("::1").await.as_deref(), Some("::1"));
    }
}

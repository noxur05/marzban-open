//! Redis writes — the entire interface between this poller and the panel.
//!
//! ## Key contract (mirrored in `app/utils/cache.py`; change both together)
//!
//! ```text
//!   extsub:payload:<sub_id>:<format>   render-ready payload, 24h TTL
//!   extsub:status:<sub_id>             JSON, last poll outcome (panel UI)
//!   extsub:identity                    JSON {hwid, user_agent} — fallback id
//!   extsub:dialect:<sub_id>:<format>   remote path segment that worked, 24h
//!   extsub:geo:<ip>                    JSON {country, cc, fronted}, 7d
//! ```
//!
//! Three rules that the panel's correctness depends on:
//!
//! 1. **Payloads carry a 24 h TTL, refreshed on every successful cycle.** A
//!    healthy poller refreshes them ~288× per TTL, so expiry can only fire
//!    after a full day of consecutive failures — and after a day in which
//!    nothing could verify those hosts, serving them is worse than the panel's
//!    documented degradation to local-only on a miss. The TTL is the bound on
//!    every *silent* staleness path at once: a remote panel that stays dead, a
//!    poller that stops running, and a payload orphaned by a delete/retarget
//!    that raced an in-flight cycle. It is deliberately long: a remote PANEL
//!    being down says nothing about its proxy hosts, so hosts must survive an
//!    ordinary remote reboot or maintenance window untouched.
//! 2. **Payloads are written unconditionally every cycle**, even when the bytes
//!    are unchanged. That is what makes Redis-only storage safe: this panel's
//!    Redis runs `--maxmemory 64mb --maxmemory-policy allkeys-lru` with no
//!    persistence, so any key can vanish at any time, and payloads are the
//!    largest values in there. Rewriting every cycle bounds the loss window to
//!    one interval instead of "until the content next changes".
//! 3. **A remote account that reports itself expired or out of quota gets its
//!    payloads DELETED, not left to age out.** The remote panel keeps serving
//!    a subscription for such an account, so the fetch itself stays green —
//!    the `subscription-userinfo` header is the only truth, and it is acted on
//!    in `poll_one` (see mod.rs). Deletion, not expiry, because this is a
//!    confirmed dead state, not an unverifiable one.
//!
//! For `v2ray` the payload is stored **decoded** (newline-joined URIs). The
//! panel has to merge before base64-encoding, so storing it encoded would mean
//! decode → merge → re-encode on every subscription fetch.
//!
//! Every function here swallows Redis errors: a Redis outage must degrade the
//! feature (users get local hosts only), never crash the poller.

use redis::AsyncCommands;

pub type Conn = redis::aio::MultiplexedConnection;

/// Staleness bound on payload keys — rule 1 above. Mirrored in the contract
/// comment in `app/utils/cache.py`.
pub const PAYLOAD_TTL_SECS: u64 = 24 * 3600;

/// Open a multiplexed connection. Returns `None` on failure so the caller can
/// log and retry on the next cycle rather than exiting — Redis coming up after
/// the poller is a normal container-start ordering.
pub async fn connect(url: &str) -> Option<Conn> {
    let client = match redis::Client::open(url) {
        Ok(c) => c,
        Err(e) => {
            tracing::error!("invalid REDIS_URL: {e}");
            return None;
        }
    };
    match client.get_multiplexed_async_connection().await {
        Ok(c) => Some(c),
        Err(e) => {
            tracing::error!("cannot reach Redis at {url}: {e}");
            None
        }
    }
}

pub async fn get(conn: &mut Conn, key: &str) -> Option<String> {
    match conn.get::<_, Option<String>>(key).await {
        Ok(v) => v,
        Err(e) => {
            tracing::debug!("redis GET {key} failed: {e}");
            None
        }
    }
}

/// SET with no expiry. Used for status and identity, which are tiny and always
/// rewritten; payloads go through `set_ex` with `PAYLOAD_TTL_SECS` (rule 1).
pub async fn set(conn: &mut Conn, key: &str, value: &str) -> bool {
    match conn.set::<_, _, ()>(key, value).await {
        Ok(()) => true,
        Err(e) => {
            tracing::warn!("redis SET {key} failed: {e}");
            false
        }
    }
}

/// SET with an expiry, for derived caches (geo, dialect) that are cheap to
/// rebuild and should not pin stale values forever.
pub async fn set_ex(conn: &mut Conn, key: &str, value: &str, ttl_secs: u64) -> bool {
    match conn.set_ex::<_, _, ()>(key, value, ttl_secs).await {
        Ok(()) => true,
        Err(e) => {
            tracing::warn!("redis SETEX {key} failed: {e}");
            false
        }
    }
}

/// EXPIRE NX on a batch of keys, as one pipelined round trip — the caller
/// repeats this every failed cycle, so per-key round trips would be the
/// dominant Redis traffic of a broken subscription. Used for payloads written
/// before the TTL rule existed (or by an older poller): a failed cycle must
/// never REFRESH the staleness bound, only add a missing one (NX). Requires
/// Redis 7 (the compose pins redis:7-alpine); on older servers the command
/// errors and is swallowed like every other Redis error here.
pub async fn expire_nx(conn: &mut Conn, keys: &[String], ttl_secs: u64) {
    if keys.is_empty() {
        return;
    }
    let mut pipe = redis::pipe();
    for key in keys {
        pipe.cmd("EXPIRE").arg(key).arg(ttl_secs).arg("NX").ignore();
    }
    if let Err(e) = pipe.query_async::<()>(conn).await {
        tracing::debug!("redis EXPIRE NX pipeline failed: {e}");
    }
}

/// DEL a batch of keys in one round trip. Used when a remote account is
/// confirmed dead (rule 3): its last-good hosts are hosts the remote panel has
/// already cut off, so leaving them to age out would keep serving them for up
/// to a day. A failed DEL self-heals — the dead check re-fires every cycle.
pub async fn del(conn: &mut Conn, keys: &[String]) -> bool {
    if keys.is_empty() {
        return true;
    }
    match conn.del::<_, ()>(keys).await {
        Ok(()) => true,
        Err(e) => {
            tracing::warn!("redis DEL {keys:?} failed: {e}");
            false
        }
    }
}

pub fn payload_key(sub_id: u32, format: &str) -> String {
    format!("extsub:payload:{sub_id}:{format}")
}

pub fn status_key(sub_id: u32) -> String {
    format!("extsub:status:{sub_id}")
}

pub fn dialect_key(sub_id: u32, format: &str) -> String {
    format!("extsub:dialect:{sub_id}:{format}")
}

#[cfg(test)]
mod tests {
    use super::*;

    // These key shapes are parsed by app/utils/cache.py. A rename here without
    // one there silently stops the panel from finding any payload — which looks
    // exactly like "nothing registered yet".
    #[test]
    fn key_shapes_match_the_panel() {
        assert_eq!(payload_key(7, "v2ray"), "extsub:payload:7:v2ray");
        assert_eq!(payload_key(7, "clash-meta"), "extsub:payload:7:clash-meta");
        assert_eq!(status_key(7), "extsub:status:7");
        assert_eq!(dialect_key(7, "v2ray"), "extsub:dialect:7:v2ray");
    }
}

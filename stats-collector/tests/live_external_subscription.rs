//! Live integration test for the external-subscription poller.
//!
//! `#[ignore]`d by default: it talks to a real remote panel, to ip-api.com and
//! to a real Redis, so it must never run as part of an ordinary `cargo test`.
//! Run it deliberately:
//!
//! ```text
//! redis-server --port 16379 --daemonize yes --save '' --appendonly no
//! EXTSUB_TEST_URL='https://panel.example/sub/<token>' \
//! EXTSUB_TEST_HWID='<hwid registered on that remote user>' \
//! EXTSUB_TEST_REDIS='redis://127.0.0.1:16379/0' \
//!   cargo test --test live_external_subscription -- --ignored --nocapture
//! ```
//!
//! It covers every part of a cycle except the single `SELECT` against the panel
//! DB: dialect discovery, the response shape check, geo verification with its
//! Redis cache, the label correction rules, and the Redis writes
//! the panel reads back.
//!
//! WARNING: never point `EXTSUB_TEST_HWID` at a HWID the remote user does not
//! already have. On these panels an unknown HWID *claims a device slot*, and a
//! claimed slot is released only by an admin block — never by time.

use marzban_stats_collector::poller::{fetch, geo, label, store};

fn env_or_skip(key: &str) -> Option<String> {
    match std::env::var(key) {
        Ok(v) if !v.trim().is_empty() => Some(v),
        _ => {
            eprintln!("skipping: {key} is not set");
            None
        }
    }
}

#[tokio::test]
#[ignore = "hits a live remote panel, ip-api.com and Redis"]
async fn mirrors_a_real_subscription_end_to_end() {
    let (url, hwid, redis_url) = match (
        env_or_skip("EXTSUB_TEST_URL"),
        env_or_skip("EXTSUB_TEST_HWID"),
        env_or_skip("EXTSUB_TEST_REDIS"),
    ) {
        (Some(u), Some(h), Some(r)) => (u, h, r),
        _ => return,
    };

    let http = fetch::build_client();
    let mut conn = store::connect(&redis_url)
        .await
        .expect("a Redis to write payloads into");

    // --- 1. Fetch the URI-list format, discovering the remote's dialect. -----
    let fetched = fetch::fetch_format(&http, &url, &hwid, "v2ray", None)
        .await
        .expect("v2ray payload");
    println!(
        "dialect segment discovered: {} (HTTP {})",
        fetched.segment, fetched.http_status
    );
    assert_eq!(fetched.http_status, 200);

    let uris: Vec<&str> = fetched
        .payload
        .lines()
        .filter(|l| l.contains("://"))
        .collect();
    assert!(!uris.is_empty(), "payload carried no proxy URIs");
    println!("{} hosts in payload", uris.len());

    // The remote user's own quota/expiry — the early warning that every local
    // user is about to lose these hosts at once.
    println!("remote userinfo: {:?}", fetched.userinfo);
    assert!(
        fetched.userinfo.is_some(),
        "expected a subscription-userinfo header to monitor the remote user with"
    );

    // --- 2. A cached dialect must be reused, not re-probed. ------------------
    let again = fetch::fetch_format(&http, &url, &hwid, "v2ray", Some(&fetched.segment))
        .await
        .expect("second fetch");
    assert_eq!(again.segment, fetched.segment);

    // --- 3. Geo-verify every host, then label. ------------------------------
    // NB: this drives label_line directly, so the cross-host relay signal (one
    // address carrying several claimed countries, computed in poller::relabel)
    // is NOT applied here. A multi-country relay therefore shows as Corrected in
    // this harness while the real cycle keeps its countries. The single-host
    // fronting signal (CDN ASN from ip-api) does apply.
    let mut verified = 0usize;
    let mut corrected = 0usize;
    let mut mismatched = 0usize;
    let mut filled = 0usize;
    let mut fronted = 0usize;

    for uri in &uris {
        let host = match label::host_of(uri) {
            Some(h) => h,
            None => continue,
        };
        let g = geo::lookup(&http, &mut conn, &host).await;
        let (out, outcome) = label::label_line(uri, g.cc.as_deref(), g.fronted);

        let claimed = label::split_line(uri)
            .map(|p| label::percent_decode(p.name_enc))
            .and_then(|n| label::claimed_cc(&n));
        println!(
            "  {host:<22} claims {:<5} ip says {:<5} fronted={:<5} -> {outcome:?}",
            claimed.as_deref().unwrap_or("-"),
            g.cc.as_deref().unwrap_or("-"),
            g.fronted
        );

        if g.fronted {
            fronted += 1;
        }
        match outcome {
            label::LabelOutcome::Verified => verified += 1,
            label::LabelOutcome::Corrected => {
                corrected += 1;
                // The label disagreed with a trustworthy IP, so it must now
                // carry the verified country and must not have stacked flags.
                assert_ne!(&out, uri, "Corrected must actually change the line");
                let name = label::percent_decode(label::split_line(&out).unwrap().name_enc);
                let expected = g.cc.as_deref().map(str::to_uppercase);
                assert_eq!(
                    label::claimed_cc(&name),
                    expected,
                    "corrected label must claim the verified country: {name}"
                );
            }
            label::LabelOutcome::KeptFronted => {
                mismatched += 1;
                // A CDN edge or multi-country relay: the IP cannot describe the
                // exit, so the remote's COUNTRY is kept — but the name is still
                // rebuilt, because the remote's text is never copied.
                let name = label::percent_decode(label::split_line(&out).unwrap().name_enc);
                assert_eq!(
                    label::claimed_cc(&name),
                    claimed,
                    "a fronted host must keep the country the remote claimed: {name}"
                );
            }
            label::LabelOutcome::Filled => {
                filled += 1;
                assert_ne!(&out, uri, "Filled must actually change the line");
                let name = label::percent_decode(label::split_line(&out).unwrap().name_enc);
                assert!(
                    label::claimed_cc(&name).is_some(),
                    "a filled label must carry a readable country: {name}"
                );
            }
            label::LabelOutcome::Unknown => {}
        }

        // The privacy contract, checked on every single line: nothing the remote
        // panel wrote may survive into what our users receive. A remote operator
        // types real user names into these remarks, and this payload goes to
        // every eligible user on this panel.
        let original_frag = label::split_line(uri).map(|p| p.name_enc).unwrap_or("");
        let original_extras = label::split_line(uri).map(|p| p.extras).unwrap_or("");
        let rebuilt = label::split_line(&out).map(|p| p.name_enc).unwrap_or("");
        let rebuilt_name = label::percent_decode(rebuilt);
        assert!(
            !out.contains(original_extras) || original_extras.is_empty(),
            "the remote's fragment extension survived: {out}"
        );
        for word in label::percent_decode(original_frag)
            .split_whitespace()
            .filter(|w| w.chars().any(|c| c.is_alphabetic()) && w.len() > 2)
        {
            // The country CODE may legitimately reappear; nothing else may.
            if word.eq_ignore_ascii_case(claimed.as_deref().unwrap_or("\u{0}")) {
                continue;
            }
            assert!(
                !rebuilt_name.contains(word),
                "remote text {word:?} leaked into the rebuilt label {rebuilt_name:?}"
            );
        }
    }
    println!(
        "labels: {verified} verified, {corrected} corrected, \
         {mismatched} kept (fronted), {filled} filled, {fronted} fronted hosts"
    );
    assert!(
        verified + corrected + mismatched + filled > 0,
        "no host could be geo-checked at all — is ip-api reachable?"
    );

    // --- 4. Geo results must be cached, or we get rate-limited. -------------
    let first_host = label::host_of(uris[0]).expect("a host on the first URI");
    let ip_or_host = first_host.clone();
    let cached = store::get(&mut conn, &format!("extsub:geo:{ip_or_host}")).await;
    assert!(
        cached.is_some(),
        "geo lookup for {ip_or_host} was not cached; a busy payload would hit \
         ip-api's rate limit every cycle"
    );

    // --- 5. Write the payload the way a cycle does, and read it back. -------
    let key = store::payload_key(999_999, "v2ray");
    assert!(store::set_ex(&mut conn, &key, &fetched.payload, store::PAYLOAD_TTL_SECS).await);
    let round_tripped = store::get(&mut conn, &key).await.expect("payload back");
    assert_eq!(round_tripped, fetched.payload);

    // Payloads must carry the 24h staleness TTL (store.rs rule 1): a healthy
    // cycle refreshes it, and a payload nothing has verified for a day must
    // stop being served rather than live forever.
    let ttl: i64 = redis::cmd("TTL")
        .arg(&key)
        .query_async(&mut conn)
        .await
        .expect("TTL");
    assert!(
        ttl > 0 && ttl <= store::PAYLOAD_TTL_SECS as i64,
        "payload keys must carry the staleness TTL (got TTL {ttl})"
    );

    let _: () = redis::cmd("DEL")
        .arg(&key)
        .query_async(&mut conn)
        .await
        .expect("cleanup");
}

#[tokio::test]
#[ignore = "hits a live remote panel"]
async fn discovers_the_dialect_for_every_supported_format() {
    let (url, hwid) = match (
        env_or_skip("EXTSUB_TEST_URL"),
        env_or_skip("EXTSUB_TEST_HWID"),
    ) {
        (Some(u), Some(h)) => (u, h),
        _ => return,
    };
    let http = fetch::build_client();

    // Proves the candidate lists cover this remote's vocabulary. A panel that
    // serves only some formats is fine — v2ray is the one that must work.
    let mut ok = Vec::new();
    for (canonical, _) in fetch::FORMATS {
        match fetch::fetch_format(&http, &url, &hwid, canonical, None).await {
            Ok(f) => {
                println!(
                    "  {canonical:<11} -> /{:<13} {} bytes",
                    f.segment,
                    f.payload.len()
                );
                ok.push(*canonical);
            }
            Err(e) => println!("  {canonical:<11} -> unavailable ({})", e.message),
        }
    }
    assert!(
        ok.contains(&"v2ray"),
        "the URI-list format is the only one the panel merges today; it must work"
    );
}

#[tokio::test]
#[ignore = "hits a live remote panel"]
async fn a_missing_hwid_is_never_mistaken_for_success() {
    let url = match env_or_skip("EXTSUB_TEST_URL") {
        Some(u) => u,
        None => return,
    };
    let http = fetch::build_client();

    // Both rejection shapes in this panel family must fail: a 403 with a JSON
    // body (newer panels) and a 200 with an EMPTY body (this repo's own fork,
    // which answers a rejected device with the reason in a header). An empty
    // HWID exercises whichever one the remote implements.
    let result = fetch::fetch_format(&http, &url, "", "v2ray", None).await;
    match result {
        Err(e) => println!(
            "rejected as expected: HTTP {:?} — {}",
            e.http_status, e.message
        ),
        Ok(f) => panic!(
            "a fetch with no HWID was accepted ({} bytes) — if the remote really \
             is open, fine, but confirm it is not returning an empty/announcement \
             body that would overwrite good hosts",
            f.payload.len()
        ),
    }
}

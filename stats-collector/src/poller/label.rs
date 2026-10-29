//! Parsing and country-labelling of proxy URIs — all pure, all unit-tested.
//!
//! The poller never *interprets* a proxy URI: the remote panel already emitted
//! a working link, and rewriting its semantics would be a way to break configs
//! for no gain. Two things only are needed here:
//!
//!   1. the host, so its country can be verified from the IP;
//!   2. the `#fragment`, which is the display name the end user sees.
//!
//! ## Labels are rebuilt, never copied
//!
//! A remark on a remote panel is free text typed by its operator, and operators
//! type real user names into it under pressure. Copying it would publish one
//! panel's user identity into every eligible user's config on this one. So the
//! only thing taken from the remote fragment is its country marker — two
//! letters, which cannot carry a name — and the display name is rebuilt as
//! `<flag> <CC> · <scheme>` from data we control. The `?serverDescription=`
//! extension is dropped for the same reason.
//!
//! ## Which country wins
//!
//! Measured against a real registered subscription (7 hosts):
//!
//! ```text
//!   198.18.42.8:1081   "🇷🇺 Russia - PUBG"  ip-api: RU  Enterprise Cloud   match
//!   198.18.43.215:222     "🇩🇪 Germany"        ip-api: DE  Hetzner            match
//!   198.18.43.215:221     "🇫🇮 Finland"        ip-api: DE  Hetzner            MISMATCH
//!   198.18.43.215:223     "🇫🇷 France"         ip-api: DE  Hetzner            MISMATCH
//!   198.18.44.0:443       "🇱🇻 Latvia"         ip-api: US  Cloudflare         MISMATCH
//! ```
//!
//! A country that disagrees with its IP is **corrected to the verified one**,
//! and a host with no country marker gets the verified one.
//!
//! The one refusal: where the IP provably cannot represent the exit, the claim
//! is kept and the disagreement only reported. Two independent signals mark
//! that, because either alone misses cases — a CDN/fronting ASN from ip-api,
//! and one address carrying several different claimed countries inside a single
//! payload. In the sample above, `198.18.44.0` (Cloudflare) and `198.18.43.215`
//! (three countries on three ports) both trip it: correcting those would have
//! replaced a correct exit country with a PoP's. Everything else is corrected.
//!
//! Rebuilding is idempotent by construction — the name is derived from country
//! and scheme, never appended to — which matters because the poller rewrites the
//! same payload every interval.
//!
//! The contract has one hard edge: it only holds for schemes whose display name
//! lives in the `#fragment`. `vmess://` keeps its remark inside a base64 JSON
//! blob (`ps`), which clients read instead of the fragment, so such a line is
//! DROPPED rather than mirrored — see `uninspectable_scheme`.

use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS};

/// What must stay escaped when re-encoding a display name. Deliberately narrow:
/// these panels emit human-readable names with spaces and punctuation, and
/// over-escaping would visibly mangle them in the client.
const NAME_ESCAPE: &AsciiSet = &CONTROLS.add(b'#').add(b'?').add(b'&').add(b'%').add(b' ');

/// A proxy URI split into the pieces this module needs.
#[derive(Debug, Clone, PartialEq)]
pub struct Line<'a> {
    /// Everything before the `#`, verbatim.
    pub prefix: &'a str,
    /// Display-name part of the fragment, still percent-encoded.
    pub name_enc: &'a str,
    /// Anything after the first `?` inside the fragment — e.g.
    /// `?serverDescription=TW9zY293`. Parsed only so it can be DISCARDED: it is
    /// free text from another panel's operator, so it can carry one of their
    /// users' names, and everything inside a `#fragment` is cosmetic (the
    /// functional parameters sit in the query, before the `#`).
    pub extras: &'a str,
}

impl Line<'_> {
    /// Rebuild the URI with a new (already percent-encoded) display name.
    ///
    /// `extras` is intentionally NOT carried over — see the field's docs. The
    /// result therefore contains no text originating from the remote panel.
    pub fn with_name(&self, name_enc: &str) -> String {
        if self.prefix.is_empty() {
            return String::new();
        }
        if name_enc.is_empty() {
            return self.prefix.to_string();
        }
        format!("{}#{}", self.prefix, name_enc)
    }
}

/// Split a proxy URI into prefix / fragment-name / fragment-extras.
pub fn split_line(line: &str) -> Option<Line<'_>> {
    let line = line.trim();
    if line.is_empty() || !line.contains("://") {
        return None;
    }
    let (prefix, frag) = match line.split_once('#') {
        Some((p, f)) => (p, f),
        None => (line, ""),
    };
    let (name_enc, extras) = match frag.find('?') {
        Some(i) => (&frag[..i], &frag[i..]),
        None => (frag, ""),
    };
    Some(Line {
        prefix,
        name_enc,
        extras,
    })
}

/// Hostname or IP literal a URI points at, with no port.
///
/// Returns `None` when the URI carries no usable host — most importantly
/// `vmess://<base64>`, where the address lives inside the encoded blob. Such a
/// line is passed through unlabelled rather than guessed at.
pub fn host_of(line: &str) -> Option<String> {
    let after_scheme = line.split_once("://")?.1;
    // Credentials may contain '@' themselves (base64 userinfo), so the host
    // starts after the LAST '@' before the first path/query/fragment
    // delimiter.
    let authority_end = after_scheme
        .find(['/', '?', '#'])
        .unwrap_or(after_scheme.len());
    let authority = &after_scheme[..authority_end];
    let hostport = match authority.rfind('@') {
        Some(i) => &authority[i + 1..],
        None => authority,
    };
    if hostport.is_empty() {
        return None;
    }

    // IPv6 literal: [::1]:443
    let host = if let Some(rest) = hostport.strip_prefix('[') {
        rest.split_once(']').map(|(h, _)| h)?
    } else {
        hostport.split(':').next()?
    };

    let host = host.trim();
    // A bare token with neither a dot nor a colon is not an address — it is
    // almost always a base64 blob we should not touch.
    if host.is_empty() || !(host.contains('.') || host.contains(':')) {
        return None;
    }
    Some(host.to_string())
}

/// Why a host must not be mirrored into a user's config, or `None` if it may be.
///
/// A remote panel is someone else's data source, and whatever it lists lands
/// verbatim in the configs of every eligible local user. Two classes have to be
/// dropped rather than merely relabelled:
///
///   * **Addresses that cannot route anywhere useful from a user's device** —
///     `0.0.0.0`, loopback, RFC1918 / CGNAT / link-local, IPv6 ULA, multicast.
///     Mirroring one gives the user an entry that silently fails, or worse
///     points their client at something on their own LAN.
///   * **`.tm` domains.** This panel serves Turkmenistan; a domestic host is
///     the one place traffic must never be routed through, and the remote panel
///     has no idea that matters to us.
///
/// Applied to the host as written in the URI. A domain that *resolves* to a
/// private address is not caught here — that would need the DNS answer, and
/// `geo::lookup` already reports such a host as having no country, so its label
/// is left alone rather than being invented.
/// Schemes whose display name is NOT in the `#fragment` and therefore cannot be
/// rebuilt by `label_line`.
///
/// `vmess://` is base64(JSON) and the operator's free-text remark lives in the
/// JSON `ps` field, which clients read INSTEAD of the fragment. Rewriting the
/// fragment leaves `ps` untouched, so the remote panel's remark — which is
/// exactly what may carry one of their users' names — would be served verbatim.
/// `host_of` also returns None for the blob, so such a line escapes host
/// exclusion and geo checks as well.
///
/// Dropped rather than decoded: mirroring a payload we have decided not to
/// inspect is what breaks the privacy contract in the first place. None of the
/// panels mirrored so far emit vmess. If one does, the fix is to decode the
/// blob, overwrite `ps` with `build_name(...)`, re-encode, and remove it here.
pub fn uninspectable_scheme(line: &str) -> Option<&'static str> {
    match scheme_of(line) {
        "vmess" => Some("vmess: remark is inside the payload, not the fragment"),
        _ => None,
    }
}

pub fn excluded_reason(host: &str) -> Option<&'static str> {
    let h = host.trim().trim_end_matches('.').to_ascii_lowercase();
    if h.is_empty() {
        return Some("empty host");
    }

    if let Ok(mut ip) = h.parse::<std::net::IpAddr>() {
        // ::ffff:127.0.0.1 is loopback wearing a v6 hat; check the v4 it maps to
        // or every v4 rule below is trivially bypassed.
        if let std::net::IpAddr::V6(v6) = ip {
            if let Some(v4) = v6.to_ipv4_mapped() {
                ip = std::net::IpAddr::V4(v4);
            }
        }
        return match ip {
            std::net::IpAddr::V4(v4) => {
                if v4.octets()[0] == 0 {
                    // The whole 0.0.0.0/8 is "this network", not just the
                    // single unspecified address.
                    Some("unspecified address (0.0.0.0/8)")
                } else if v4.is_loopback() {
                    Some("loopback address")
                } else if v4.is_private() {
                    Some("private address (RFC1918)")
                } else if v4.is_link_local() {
                    Some("link-local address")
                } else if v4.is_broadcast() {
                    Some("broadcast address")
                } else if v4.is_multicast() {
                    Some("multicast address")
                } else if v4.octets()[0] == 100 && (64..=127).contains(&v4.octets()[1]) {
                    Some("CGNAT address (100.64/10)")
                } else if v4.octets()[0] >= 240 {
                    Some("reserved address (240/4)")
                } else {
                    None
                }
            }
            std::net::IpAddr::V6(v6) => {
                if v6.is_unspecified() {
                    Some("unspecified address (::)")
                } else if v6.is_loopback() {
                    Some("loopback address")
                } else if v6.is_multicast() {
                    Some("multicast address")
                } else if (v6.segments()[0] & 0xfe00) == 0xfc00 {
                    Some("unique local address (fc00::/7)")
                } else if (v6.segments()[0] & 0xffc0) == 0xfe80 {
                    Some("link-local address")
                } else {
                    None
                }
            }
        };
    }

    // Not an IP literal, so treat it as a name.
    if h == "localhost" || h.ends_with(".localhost") {
        return Some("localhost");
    }
    if h.ends_with(".tm") {
        return Some("domestic .tm domain");
    }
    None
}

/// ISO-3166 alpha-2 → flag emoji, via regional indicator symbols.
/// Ported from the user's vpn-watchdog (`src/utils.rs::flag_emoji_from_cc`) so
/// both projects render labels identically.
pub fn flag_from_cc(cc: &str) -> String {
    cc.chars()
        .filter_map(|c| {
            let upper = c.to_ascii_uppercase();
            if upper.is_ascii_alphabetic() {
                char::from_u32(0x1F1E6 + (upper as u32 - 'A' as u32))
            } else {
                None
            }
        })
        .collect()
}

/// Country code a display name already claims, if any.
///
/// Recognises the two forms these panels emit: a flag emoji (a pair of
/// regional indicator symbols, which is what `flag_from_cc` produces) and an
/// ASCII `[XX]` prefix. A name claiming a country in prose only ("Germany"
/// with no flag) reads as unclaimed — filling a flag in front of it is
/// harmless, whereas trying to parse country names in six languages is not.
pub fn claimed_cc(name: &str) -> Option<String> {
    let indicators: Vec<char> = name
        .chars()
        .filter(|c| ('\u{1F1E6}'..='\u{1F1FF}').contains(c))
        .collect();
    if indicators.len() >= 2 {
        let a = (indicators[0] as u32 - 0x1F1E6) as u8 + b'A';
        let b = (indicators[1] as u32 - 0x1F1E6) as u8 + b'A';
        return Some(String::from_utf8_lossy(&[a, b]).to_string());
    }

    let bytes = name.as_bytes();
    if bytes.len() >= 4
        && bytes[0] == b'['
        && bytes[3] == b']'
        && bytes[1].is_ascii_alphabetic()
        && bytes[2].is_ascii_alphabetic()
    {
        return Some(name[1..3].to_ascii_uppercase());
    }
    None
}

/// Outcome of labelling one line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LabelOutcome {
    /// The remote's country marker agrees with the IP.
    Verified,
    /// The remote's country marker disagrees with a trustworthy IP; the IP won.
    Corrected,
    /// The remote's marker disagrees with the IP, but the IP cannot represent
    /// the exit (CDN edge, or one address serving several countries), so the
    /// remote's country was used. Rewriting here would replace a correct exit
    /// country with a PoP's.
    KeptFronted,
    /// The remote claimed no country and the IP supplied one.
    Filled,
    /// No country could be established at all.
    Unknown,
}

/// Rebuild one line's display name from scratch.
///
/// ## The remote's label text is never copied
///
/// A remark on another panel is free text typed by its operator, and operators
/// type real user names into it — especially in a hurry. Mirroring that text
/// would publish one panel's user identity into the configs of every eligible
/// user on this one. So the *only* thing taken from the remote fragment is its
/// country marker: a two-letter code cannot carry a name. Everything else — the
/// descriptive text and any `?serverDescription=`-style extension, which live
/// inside the `#fragment` and are purely cosmetic — is discarded.
///
/// What gets written instead is built entirely from data we control:
///
/// ```text
///     🇩🇪 DE · hysteria2
/// ```
///
/// country (verified from the IP, or the remote's marker when the IP is a CDN
/// edge) plus the URI scheme, which distinguishes several hosts in the same
/// country without revealing anything about anyone. `relabel` appends a numeric
/// suffix if two hosts still collide.
///
/// `verified_cc` is the country the host's IP resolves to, or `None` when geo
/// lookup produced nothing. `fronted` marks a host whose IP cannot represent the
/// exit.
pub fn label_line(line: &str, verified_cc: Option<&str>, fronted: bool) -> (String, LabelOutcome) {
    let parts = match split_line(line) {
        Some(p) => p,
        None => return (line.to_string(), LabelOutcome::Unknown),
    };
    let claim = claimed_cc(&percent_decode(parts.name_enc));

    let (cc, outcome) = match (claim.as_deref(), verified_cc) {
        (Some(c), Some(v)) if c.eq_ignore_ascii_case(v) => (Some(v), LabelOutcome::Verified),
        // A CDN edge or a multi-country relay: the IP describes a PoP, so the
        // remote's country is the better of the two.
        (Some(c), Some(_)) if fronted => (Some(c), LabelOutcome::KeptFronted),
        (Some(_), Some(v)) => (Some(v), LabelOutcome::Corrected),
        // Geo failed; the remote's marker is all there is.
        (Some(c), None) => (Some(c), LabelOutcome::Unknown),
        // Nothing claimed and the IP is an edge: naming the edge's country would
        // be actively misleading.
        (None, Some(_)) if fronted => (None, LabelOutcome::Unknown),
        (None, Some(v)) => (Some(v), LabelOutcome::Filled),
        (None, None) => (None, LabelOutcome::Unknown),
    };

    (
        parts.with_name(&encode_name(&build_name(cc, scheme_of(line)))),
        outcome,
    )
}

/// URI scheme, used as the only disambiguator between hosts in one country.
/// Safe by construction: it comes from the protocol, not from anyone's text.
pub fn scheme_of(line: &str) -> &str {
    match line.split_once("://") {
        Some((s, _)) => s.trim(),
        None => "",
    }
}

/// `🇩🇪 DE · hysteria2`, or `🌐 · hysteria2` when no country is known.
pub fn build_name(cc: Option<&str>, scheme: &str) -> String {
    let head = match cc {
        Some(c) => {
            let c = c.to_ascii_uppercase();
            format!("{} {}", flag_from_cc(&c), c)
        }
        // Deliberately bland, and deliberately not a guess.
        None => "🌐".to_string(),
    };
    if scheme.is_empty() {
        head
    } else {
        format!("{head} · {scheme}")
    }
}

pub fn encode_name(name: &str) -> String {
    utf8_percent_encode(name, NAME_ESCAPE).to_string()
}

/// Minimal percent-decoder. `percent_encoding`'s decoder needs a borrow dance
/// for lossy UTF-8; this is short enough to own outright and keeps the return
/// type a plain `String`.
pub fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hi = (bytes[i + 1] as char).to_digit(16);
            let lo = (bytes[i + 2] as char).to_digit(16);
            if let (Some(h), Some(l)) = (hi, lo) {
                out.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    // The real fragment shape observed on a live subscription: percent-encoded
    // flag + name, then a Happ-specific `?serverDescription=` extension.
    const RU: &str = "ss://Y2hhY2hh@198.18.42.8:1081#%F0%9F%87%B7%F0%9F%87%BA%20Russia%20-%20PUBG?serverDescription=TW9zY293";

    #[test]
    fn vmess_is_refused_because_its_remark_cannot_be_rebuilt() {
        let l = "vmess://eyJwcyI6IkJhZ3R5eWFyX2hhY2tlciJ9";
        assert!(uninspectable_scheme(l).is_some());
        // Every scheme whose name lives in the fragment IS inspectable.
        for ok in [
            "vless://u@1.2.3.4:443#x",
            "ss://x@1.2.3.4:99#x",
            "trojan://p@1.2.3.4:443#x",
            "hysteria2://p@1.2.3.4:443#x",
        ] {
            assert!(uninspectable_scheme(ok).is_none(), "{ok}");
        }
    }

    #[test]
    fn ipv4_mapped_ipv6_cannot_smuggle_a_loopback_or_lan_host() {
        for h in [
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.5",
            "::ffff:192.168.1.1",
            "::ffff:0.0.0.0",
        ] {
            assert!(excluded_reason(h).is_some(), "{h} should be excluded");
        }
        // A mapped PUBLIC address is still fine.
        assert_eq!(excluded_reason("::ffff:8.8.8.8"), None);
    }

    #[test]
    fn the_whole_zero_slash_eight_is_excluded() {
        assert!(excluded_reason("0.0.0.0").is_some());
        assert!(excluded_reason("0.1.2.3").is_some());
    }

    #[test]
    fn excludes_unroutable_ipv4() {
        for (h, why) in [
            ("0.0.0.0", "unspecified"),
            ("127.0.0.1", "loopback"),
            ("127.53.1.9", "loopback"),
            ("10.0.0.5", "private"),
            ("172.16.4.1", "private"),
            ("172.31.255.254", "private"),
            ("192.168.1.1", "private"),
            ("169.254.10.10", "link-local"),
            ("100.64.0.1", "CGNAT"),
            ("255.255.255.255", "broadcast"),
            ("239.1.2.3", "multicast"),
            ("245.0.0.1", "reserved"),
        ] {
            let r = excluded_reason(h).unwrap_or_else(|| panic!("{h} should be excluded"));
            assert!(r.contains(why), "{h}: reason {r:?} should mention {why:?}");
        }
    }

    #[test]
    fn excludes_unroutable_ipv6() {
        for h in ["::", "::1", "fc00::1", "fd12:3456::1", "fe80::1", "ff02::1"] {
            assert!(excluded_reason(h).is_some(), "{h} should be excluded");
        }
    }

    #[test]
    fn excludes_localhost_and_domestic_domains() {
        assert!(excluded_reason("localhost").is_some());
        assert!(excluded_reason("LOCALHOST").is_some());
        assert!(excluded_reason("foo.localhost").is_some());
        assert_eq!(excluded_reason("node.tm"), Some("domestic .tm domain"));
        assert_eq!(
            excluded_reason("A.SUB.EXAMPLE.TM"),
            Some("domestic .tm domain")
        );
        // Trailing dot is a valid FQDN form and must not smuggle one past us.
        assert_eq!(excluded_reason("node.tm."), Some("domestic .tm domain"));
    }

    #[test]
    fn keeps_ordinary_public_hosts() {
        // Every host from the real measured payload must survive.
        for h in [
            "198.18.42.8",
            "198.18.43.215",
            "198.18.44.0",
            "node.example.com",
            "2001:db8::1",
            "9.9.9.9",
        ] {
            assert_eq!(excluded_reason(h), None, "{h} must not be excluded");
        }
    }

    #[test]
    fn tm_check_matches_the_tld_not_a_substring() {
        // ".tm" only as the final label — these are ordinary foreign hosts.
        assert_eq!(excluded_reason("tm.example.com"), None);
        assert_eq!(excluded_reason("nodetm"), None);
        assert_eq!(excluded_reason("my.tmobile.net"), None);
        assert_eq!(excluded_reason("example.tmp"), None);
    }

    // ---- label rebuilding: no text from the remote panel survives ----

    #[test]
    fn agreeing_country_is_kept_but_the_name_is_still_rebuilt() {
        let (out, outcome) = label_line(RU, Some("RU"), false);
        assert_eq!(outcome, LabelOutcome::Verified);
        assert_eq!(
            percent_decode(split_line(&out).unwrap().name_enc),
            "🇷🇺 RU · ss"
        );
    }

    #[test]
    fn the_remotes_free_text_never_survives() {
        // "Russia - PUBG" is harmless; the next operator's "Russia - Aman" is
        // not. Neither is copied, so neither can leak.
        let (out, _) = label_line(RU, Some("RU"), false);
        let name = percent_decode(split_line(&out).unwrap().name_enc);
        assert!(!name.contains("PUBG"), "remote text leaked into {name}");
        assert!(!out.contains("PUBG"), "remote text leaked into {out}");
    }

    #[test]
    fn the_serverdescription_extension_is_dropped() {
        // It lives inside the #fragment (cosmetic) and is free text, so it is a
        // second place a user's name could hide.
        assert!(RU.contains("?serverDescription="), "fixture must carry one");
        let (out, _) = label_line(RU, Some("RU"), false);
        assert!(!out.contains("serverDescription"), "{out}");
        assert!(!out.contains("TW9zY293"), "{out}");
    }

    #[test]
    fn a_name_that_is_someones_identity_is_discarded() {
        let l = "vless://uuid@1.2.3.4:443#Germany%20-%20Aman%20Didarov?serverDescription=YXJpZg==";
        let (out, _) = label_line(l, Some("DE"), false);
        assert!(!out.contains("Aman"), "{out}");
        assert!(!out.contains("Didarov"), "{out}");
        assert!(!out.contains("YXJpZg=="), "{out}");
        assert_eq!(
            percent_decode(split_line(&out).unwrap().name_enc),
            "🇩🇪 DE · vless"
        );
    }

    #[test]
    fn disagreeing_country_is_corrected_when_the_ip_is_trustworthy() {
        let l = "hysteria2://pass@198.18.43.215:221/?sni=x#%F0%9F%87%AB%F0%9F%87%AE%20Finland";
        let (out, outcome) = label_line(l, Some("DE"), false);
        assert_eq!(outcome, LabelOutcome::Corrected);
        assert_eq!(
            percent_decode(split_line(&out).unwrap().name_enc),
            "🇩🇪 DE · hysteria2"
        );
    }

    #[test]
    fn fronted_host_keeps_the_remotes_country_but_not_its_text() {
        // Cloudflare edge in front of a Latvian exit: LV is the better country,
        // and it is only two letters, so keeping it leaks nothing.
        let l = "vless://uuid@198.18.44.0:443?type=tcp#%F0%9F%87%B1%F0%9F%87%BB%20Latvia%20Riga";
        let (out, outcome) = label_line(l, Some("US"), true);
        assert_eq!(outcome, LabelOutcome::KeptFronted);
        let name = percent_decode(split_line(&out).unwrap().name_enc);
        assert_eq!(name, "🇱🇻 LV · vless");
        assert!(!name.contains("Riga"));
    }

    #[test]
    fn unlabelled_host_gets_the_verified_country() {
        let l = "vless://uuid@1.2.3.4:443#Node%201";
        let (out, outcome) = label_line(l, Some("nl"), false);
        assert_eq!(outcome, LabelOutcome::Filled);
        assert_eq!(
            percent_decode(split_line(&out).unwrap().name_enc),
            "🇳🇱 NL · vless"
        );
    }

    #[test]
    fn fronted_and_unlabelled_gets_no_country_rather_than_a_guess() {
        let l = "vless://uuid@198.18.44.0:443#Node";
        let (out, outcome) = label_line(l, Some("US"), true);
        assert_eq!(outcome, LabelOutcome::Unknown);
        assert_eq!(
            percent_decode(split_line(&out).unwrap().name_enc),
            "🌐 · vless"
        );
    }

    #[test]
    fn geo_failure_falls_back_to_the_remotes_country_marker() {
        let (out, outcome) = label_line(RU, None, false);
        assert_eq!(outcome, LabelOutcome::Unknown);
        assert_eq!(
            percent_decode(split_line(&out).unwrap().name_enc),
            "🇷🇺 RU · ss"
        );
    }

    #[test]
    fn relabelling_is_idempotent() {
        // The poller rewrites the same payload every interval; a second pass
        // must produce byte-identical output.
        let (once, _) = label_line(RU, Some("RU"), false);
        let (twice, _) = label_line(&once, Some("RU"), false);
        assert_eq!(once, twice);
    }

    #[test]
    fn unparseable_line_is_passed_through_untouched() {
        let (out, outcome) = label_line("garbage", Some("DE"), false);
        assert_eq!(outcome, LabelOutcome::Unknown);
        assert_eq!(out, "garbage");
    }

    #[test]
    fn scheme_is_read_from_the_uri() {
        assert_eq!(scheme_of(RU), "ss");
        assert_eq!(scheme_of("hysteria2://a@b.c:1#x"), "hysteria2");
        assert_eq!(scheme_of("no-scheme"), "");
    }

    #[test]
    fn build_name_shapes() {
        assert_eq!(build_name(Some("de"), "vless"), "🇩🇪 DE · vless");
        assert_eq!(build_name(None, "ss"), "🌐 · ss");
        assert_eq!(build_name(Some("DE"), ""), "🇩🇪 DE");
    }

    #[test]
    fn extracts_host_from_ss_with_base64_userinfo() {
        assert_eq!(host_of(RU).as_deref(), Some("198.18.42.8"));
    }

    #[test]
    fn extracts_host_from_vless_with_query() {
        let l = "vless://uuid-1234@198.18.44.0:443?type=tcp&security=reality#%F0%9F%87%B1%F0%9F%87%BB%20Latvia";
        assert_eq!(host_of(l).as_deref(), Some("198.18.44.0"));
    }

    #[test]
    fn extracts_host_from_hysteria2() {
        let l = "hysteria2://pass@198.18.43.215:221/?sni=x#%F0%9F%87%AB%F0%9F%87%AE%20Finland";
        assert_eq!(host_of(l).as_deref(), Some("198.18.43.215"));
    }

    #[test]
    fn extracts_ipv6_literal_without_brackets_or_port() {
        let l = "vless://uuid@[2001:db8::1]:443?type=tcp#name";
        assert_eq!(host_of(l).as_deref(), Some("2001:db8::1"));
    }

    #[test]
    fn extracts_domain_host() {
        let l = "trojan://pw@node.example.com:443#DE";
        assert_eq!(host_of(l).as_deref(), Some("node.example.com"));
    }

    #[test]
    fn refuses_vmess_base64_blob() {
        // vmess carries its address inside the blob; there is no host to read.
        assert_eq!(host_of("vmess://eyJhZGQiOiIxLjIuMy40In0"), None);
    }

    #[test]
    fn splits_fragment_into_name_and_extras() {
        let p = split_line(RU).unwrap();
        assert_eq!(p.name_enc, "%F0%9F%87%B7%F0%9F%87%BA%20Russia%20-%20PUBG");
        assert_eq!(p.extras, "?serverDescription=TW9zY293");
    }

    #[test]
    fn handles_line_with_no_fragment() {
        let p = split_line("vless://uuid@1.2.3.4:443?type=tcp").unwrap();
        assert_eq!(p.name_enc, "");
        assert_eq!(p.extras, "");
        assert_eq!(p.with_name(""), "vless://uuid@1.2.3.4:443?type=tcp");
    }

    #[test]
    fn ignores_non_uri_lines() {
        assert!(split_line("").is_none());
        assert!(split_line("   ").is_none());
        assert!(split_line("not a uri").is_none());
    }

    #[test]
    fn decodes_percent_escapes_including_multibyte() {
        assert_eq!(
            percent_decode("%F0%9F%87%B7%F0%9F%87%BA%20Russia"),
            "🇷🇺 Russia"
        );
        assert_eq!(percent_decode("plain name"), "plain name");
        // A stray, un-decodable '%' must survive rather than eat the string.
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(percent_decode("50%zz"), "50%zz");
    }

    #[test]
    fn reads_claimed_country_from_flag_emoji() {
        assert_eq!(claimed_cc("🇷🇺 Russia - PUBG").as_deref(), Some("RU"));
        assert_eq!(claimed_cc("🇩🇪 Germany").as_deref(), Some("DE"));
        assert_eq!(claimed_cc("🇫🇮 Finland").as_deref(), Some("FI"));
    }

    #[test]
    fn reads_claimed_country_from_bracket_form() {
        assert_eq!(claimed_cc("[NL] Amsterdam").as_deref(), Some("NL"));
    }

    #[test]
    fn treats_prose_only_name_as_unclaimed() {
        // No flag, no bracket: safe to prepend a verified one.
        assert_eq!(claimed_cc("Germany"), None);
        assert_eq!(claimed_cc(""), None);
    }

    #[test]
    fn flag_round_trips_through_claimed_cc() {
        for cc in ["RU", "DE", "FI", "FR", "LV", "US", "NL"] {
            let name = format!("{} something", flag_from_cc(cc));
            assert_eq!(claimed_cc(&name).as_deref(), Some(cc), "cc={cc}");
        }
    }
}

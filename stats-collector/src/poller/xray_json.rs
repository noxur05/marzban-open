//! Turning an Xray-JSON subscription into the URI list the panel can merge.
//!
//! ## Why this exists
//!
//! Some panels in this family serve ONLY Xray JSON. Measured live against
//! example-f.com on 2026-08-14, every other route is closed:
//!
//! ```text
//!   /links         406 {"detail":"Client not supported"}
//!   /links_base64  500 Internal Server Error
//!   /v2ray         422 (not in its vocabulary)
//!   /clash /clash_meta /outline   406
//!   /sing_box      500
//!   /xray          200  <- the only thing that answers
//!   bare URL       200 JSON for Happ/*, 406 for every other User-Agent
//! ```
//!
//! The panel merges `v2ray` (a URI list) and nothing else, and this fork's
//! Happ users receive that URI list rather than JSON (`USE_CUSTOM_JSON_FOR_HAPP`
//! is false), so a JSON-to-JSON merge would reach nobody. Converting here — at
//! the edge, into the one canonical format — keeps `MERGEABLE_FORMATS` at
//! `("v2ray",)` and means the existing unroutable/`.tm` filter, geo
//! verification and label rebuild all apply unchanged, because they operate on
//! URI lines.
//!
//! ## What is converted, and what is dropped
//!
//! Each element of the top-level array is one server profile: a `proxy`
//! outbound plus `direct`/`block`/`dns` boilerplate we discard, and a
//! `remarks` label. Only the proxy outbound matters.
//!
//! Anything this module cannot express as a URI is DROPPED with a reason
//! rather than guessed at — a malformed URI in a user's subscription is worse
//! than a missing host, because some clients reject the whole list.

use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS};
use serde_json::Value;

use super::label;

/// Escape set for the userinfo position (`scheme://HERE@host:port`).
///
/// Stricter than `label::encode_name`, which was written for display names and
/// leaves `@` and `:` literal. A trojan or shadowsocks password containing `@`
/// would otherwise split the authority and point the client at a different
/// host entirely; `:` would corrupt the port. Both characters are legal in a
/// password and neither is theoretical.
const USERINFO_ESCAPE: &AsciiSet = &CONTROLS
    .add(b'@')
    .add(b':')
    .add(b'/')
    .add(b'?')
    .add(b'#')
    .add(b'&')
    .add(b'=')
    .add(b'%')
    .add(b' ');

fn encode_userinfo(s: &str) -> String {
    utf8_percent_encode(s, USERINFO_ESCAPE).to_string()
}

/// Result of converting one JSON document.
#[derive(Debug, Default)]
pub struct Converted {
    /// URI lines, ready to go through `relabel` like any other payload.
    pub lines: Vec<String>,
    /// Profiles that could not be expressed, with why. Surfaced in the status
    /// so a silently shrinking host list is visible.
    pub skipped: Vec<String>,
}

/// Convert an Xray-JSON subscription body into URI lines.
///
/// Accepts either the array form (one element per server, which is what this
/// family emits) or a single bare config object.
pub fn to_uri_lines(body: &str) -> Result<Converted, String> {
    let parsed: Value = serde_json::from_str(body).map_err(|e| format!("not valid JSON: {e}"))?;

    let profiles: Vec<&Value> = match &parsed {
        Value::Array(items) => items.iter().collect(),
        Value::Object(_) => vec![&parsed],
        _ => return Err("JSON is neither an object nor an array".to_string()),
    };

    let mut out = Converted::default();
    for (idx, profile) in profiles.iter().enumerate() {
        // The label the remote chose. Passed through as the fragment purely so
        // the geo layer can read a claimed country off it; `label_line`
        // REBUILDS the visible name from country + scheme, so no remote text
        // reaches a user (see label.rs — that is a hard requirement, remote
        // operators put real customer names in remarks).
        let remark = profile
            .get("remarks")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();

        let Some(outbound) = proxy_outbound(profile) else {
            out.skipped
                .push(format!("profile {idx}: no proxy outbound"));
            continue;
        };
        match outbound_to_uri(outbound, &remark) {
            Ok(line) => out.lines.push(line),
            Err(why) => out.skipped.push(format!("profile {idx}: {why}")),
        }
    }
    Ok(out)
}

/// The outbound that actually carries traffic.
///
/// Prefers `tag == "proxy"` (the convention in this family) and otherwise
/// falls back to the first outbound with a protocol we know how to express, so
/// a panel that tags things differently still works.
fn proxy_outbound(profile: &Value) -> Option<&Value> {
    let outbounds = profile.get("outbounds")?.as_array()?;
    outbounds
        .iter()
        .find(|o| o.get("tag").and_then(Value::as_str) == Some("proxy"))
        .or_else(|| {
            outbounds.iter().find(|o| {
                matches!(
                    o.get("protocol").and_then(Value::as_str),
                    Some("vless" | "trojan" | "shadowsocks" | "hysteria")
                )
            })
        })
}

fn outbound_to_uri(outbound: &Value, remark: &str) -> Result<String, String> {
    let protocol = outbound
        .get("protocol")
        .and_then(Value::as_str)
        .ok_or("outbound has no protocol")?;
    let settings = outbound.get("settings").unwrap_or(&Value::Null);
    let stream = outbound.get("streamSettings").unwrap_or(&Value::Null);

    match protocol {
        "vless" => vless(settings, stream, remark),
        "trojan" => trojan(settings, stream, remark),
        "shadowsocks" => shadowsocks(settings, remark),
        "hysteria" => hysteria(settings, stream, remark),
        // vmess:// is a base64 blob whose label cannot be rebuilt without
        // decoding it, so `label::uninspectable_scheme` drops it downstream
        // anyway. Skipping here makes the reason visible instead of silent.
        "vmess" => Err("vmess is not mirrored (its label cannot be rebuilt)".to_string()),
        other => Err(format!("unsupported protocol '{other}'")),
    }
}

fn vless(settings: &Value, stream: &Value, remark: &str) -> Result<String, String> {
    let vnext = settings
        .get("vnext")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .ok_or("vless outbound has no vnext")?;
    let address = str_field(vnext, "address").ok_or("vless vnext has no address")?;
    let port = num_field(vnext, "port").ok_or("vless vnext has no port")?;
    let user = vnext
        .get("users")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .ok_or("vless vnext has no users")?;
    let id = str_field(user, "id").ok_or("vless user has no id")?;

    let mut params = stream_params(stream);
    params.push((
        "encryption",
        str_field(user, "encryption").unwrap_or_else(|| "none".to_string()),
    ));
    if let Some(flow) = str_field(user, "flow") {
        if !flow.is_empty() {
            params.push(("flow", flow));
        }
    }
    Ok(build_uri("vless", &id, &address, port, &params, remark))
}

fn trojan(settings: &Value, stream: &Value, remark: &str) -> Result<String, String> {
    let server = settings
        .get("servers")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .ok_or("trojan outbound has no servers")?;
    let address = str_field(server, "address").ok_or("trojan server has no address")?;
    let port = num_field(server, "port").ok_or("trojan server has no port")?;
    let password = str_field(server, "password").ok_or("trojan server has no password")?;
    let params = stream_params(stream);
    Ok(build_uri(
        "trojan", &password, &address, port, &params, remark,
    ))
}

fn shadowsocks(settings: &Value, remark: &str) -> Result<String, String> {
    use base64::Engine;
    let server = settings
        .get("servers")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .ok_or("shadowsocks outbound has no servers")?;
    let address = str_field(server, "address").ok_or("shadowsocks server has no address")?;
    let port = num_field(server, "port").ok_or("shadowsocks server has no port")?;
    let method = str_field(server, "method").ok_or("shadowsocks server has no method")?;
    let password = str_field(server, "password").ok_or("shadowsocks server has no password")?;
    // ss:// carries method:password as base64 in the userinfo position.
    let userinfo = base64::engine::general_purpose::STANDARD.encode(format!("{method}:{password}"));
    Ok(format!(
        "ss://{userinfo}@{address}:{port}#{}",
        label::encode_name(remark)
    ))
}

/// Hysteria **v2** only.
///
/// The JSON shape is a Happ/Xray extension rather than upstream Xray: the
/// address and port live in `settings`, and the credential in
/// `streamSettings.hysteriaSettings.auth`.
///
/// `insecure=1` is emitted whenever a certificate pin is present, and that
/// pairing is deliberate: these servers present a self-signed certificate
/// under a borrowed SNI (`google.com` on the live payload), so ordinary chain
/// validation cannot succeed. The pin is what actually authenticates the
/// server, and without `insecure` the client would reject it outright.
fn hysteria(settings: &Value, stream: &Value, remark: &str) -> Result<String, String> {
    let hy = stream
        .get("hysteriaSettings")
        .ok_or("hysteria outbound has no hysteriaSettings")?;
    let version = num_field(hy, "version")
        .or_else(|| num_field(settings, "version"))
        .unwrap_or(2);
    if version != 2 {
        return Err(format!(
            "hysteria v{version} is not mirrored (only v2 has a stable URI form)"
        ));
    }
    let address = str_field(settings, "address").ok_or("hysteria settings have no address")?;
    let port = num_field(settings, "port").ok_or("hysteria settings have no port")?;
    let auth = str_field(hy, "auth").ok_or("hysteria settings have no auth")?;

    let tls = stream.get("tlsSettings").unwrap_or(&Value::Null);
    let mut params: Vec<(&str, String)> = Vec::new();
    if let Some(sni) = str_field(tls, "serverName") {
        params.push(("sni", sni));
    }
    if let Some(pin) = str_field(tls, "pinnedPeerCertSha256") {
        params.push(("pinSHA256", pin));
        params.push(("insecure", "1".to_string()));
    }
    if let Some(obfs) = str_field(hy, "obfs") {
        params.push(("obfs", "salamander".to_string()));
        params.push(("obfs-password", obfs));
    }
    Ok(build_uri(
        "hysteria2",
        &auth,
        &address,
        port,
        &params,
        remark,
    ))
}

/// Transport and TLS parameters shared by vless and trojan.
fn stream_params(stream: &Value) -> Vec<(&'static str, String)> {
    let mut params: Vec<(&'static str, String)> = Vec::new();
    let network = str_field(stream, "network").unwrap_or_else(|| "tcp".to_string());
    params.push(("type", network.clone()));

    let security = str_field(stream, "security").unwrap_or_else(|| "none".to_string());
    if security != "none" {
        params.push(("security", security.clone()));
    }

    let tls = stream
        .get("tlsSettings")
        .or_else(|| stream.get("realitySettings"))
        .unwrap_or(&Value::Null);
    if let Some(sni) = str_field(tls, "serverName") {
        params.push(("sni", sni));
    }
    if let Some(fp) = str_field(tls, "fingerprint") {
        params.push(("fp", fp));
    }
    if let Some(alpn) = tls.get("alpn").and_then(Value::as_array) {
        let joined: Vec<String> = alpn
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect();
        if !joined.is_empty() {
            params.push(("alpn", joined.join(",")));
        }
    }
    // REALITY carries its keys here rather than in tlsSettings.
    if let Some(pbk) = str_field(tls, "publicKey") {
        params.push(("pbk", pbk));
    }
    if let Some(sid) = str_field(tls, "shortId") {
        params.push(("sid", sid));
    }
    if let Some(spx) = str_field(tls, "spiderX") {
        params.push(("spx", spx));
    }

    match network.as_str() {
        "ws" | "httpupgrade" => {
            let ws = stream
                .get("wsSettings")
                .or_else(|| stream.get("httpupgradeSettings"))
                .unwrap_or(&Value::Null);
            if let Some(path) = str_field(ws, "path") {
                params.push(("path", path));
            }
            // The Host header lives either at the top of the settings object or
            // inside `headers`, depending on the panel's Xray version.
            let host = str_field(ws, "host").or_else(|| {
                ws.get("headers")
                    .and_then(|h| h.get("Host"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
            });
            if let Some(host) = host {
                params.push(("host", host));
            }
        }
        "grpc" => {
            let grpc = stream.get("grpcSettings").unwrap_or(&Value::Null);
            if let Some(sn) = str_field(grpc, "serviceName") {
                params.push(("serviceName", sn));
            }
        }
        _ => {}
    }
    params
}

fn build_uri(
    scheme: &str,
    userinfo: &str,
    address: &str,
    port: u64,
    params: &[(&str, String)],
    remark: &str,
) -> String {
    let query: Vec<String> = params
        .iter()
        .filter(|(_, v)| !v.is_empty())
        // encode_name escapes `#`, `?`, `&`, `%` and space — the separators
        // that would corrupt a query — while leaving `/` literal, which is what
        // a `path=` value wants.
        .map(|(k, v)| format!("{k}={}", label::encode_name(v)))
        .collect();
    // An IPv6 literal has to be bracketed or the port is unparseable.
    let host = if address.contains(':') && !address.starts_with('[') {
        format!("[{address}]")
    } else {
        address.to_string()
    };
    let mut uri = format!("{scheme}://{}@{host}:{port}", encode_userinfo(userinfo));
    if !query.is_empty() {
        uri.push('?');
        uri.push_str(&query.join("&"));
    }
    uri.push('#');
    uri.push_str(&label::encode_name(remark));
    uri
}

fn str_field(v: &Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

fn num_field(v: &Value, key: &str) -> Option<u64> {
    v.get(key).and_then(Value::as_u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Trimmed from the live example-f.com payload (2026-08-14): eight
    /// hysteria v2 profiles and one vless+ws, which is the shape this module
    /// was written against.
    const LIVE: &str = r#"[
      {
        "outbounds": [
          {
            "protocol": "hysteria", "tag": "proxy",
            "settings": {"version": 2, "address": "198.18.40.75", "port": 200},
            "streamSettings": {
              "network": "hysteria", "security": "tls",
              "tlsSettings": {
                "serverName": "google.com",
                "pinnedPeerCertSha256": "EC347EC6327A22CA720719CBDE18D2D20C5D66C7B51DA8CE47C9ADBF26E57073",
                "alpn": ["h3"]
              },
              "hysteriaSettings": {"version": 2, "auth": "EXAMPLEAUTHEXAMPLEAUTHEXAMPLE000"}
            }
          },
          {"protocol": "freedom", "tag": "direct"}
        ],
        "remarks": "🇬🇧 UK - High Speed ⚡"
      },
      {
        "outbounds": [
          {
            "protocol": "vless", "tag": "proxy",
            "settings": {"vnext": [{
              "address": "198.18.41.125", "port": 443,
              "users": [{"id": "00000000-0000-4000-8000-000000000001", "encryption": "none"}]
            }]},
            "streamSettings": {
              "network": "ws", "security": "tls",
              "tlsSettings": {"serverName": "m.example-g.com", "fingerprint": "chrome", "alpn": ["http/1.1"]},
              "wsSettings": {"path": "/", "host": "m.example-g.com"}
            }
          }
        ],
        "remarks": "🇪🇺 EU - Cloudflare ♻️"
      }
    ]"#;

    #[test]
    fn converts_the_live_payload() {
        let out = to_uri_lines(LIVE).unwrap();
        assert_eq!(out.lines.len(), 2, "skipped: {:?}", out.skipped);
        assert!(out.skipped.is_empty());

        let hy = &out.lines[0];
        assert!(hy.starts_with("hysteria2://EXAMPLEAUTHEXAMPLEAUTHEXAMPLE000@198.18.40.75:200?"));
        assert!(hy.contains("sni=google.com"));
        assert!(hy.contains("pinSHA256=EC347EC6"));
        // A pinned self-signed cert under a borrowed SNI cannot pass ordinary
        // chain validation; the pin is what authenticates it.
        assert!(hy.contains("insecure=1"));

        let vl = &out.lines[1];
        assert!(vl.starts_with("vless://00000000-0000-4000-8000-000000000001@198.18.41.125:443?"));
        assert!(vl.contains("type=ws"));
        assert!(vl.contains("security=tls"));
        assert!(vl.contains("sni=m.example-g.com"));
        assert!(vl.contains("fp=chrome"));
        assert!(vl.contains("host=m.example-g.com"));
        assert!(vl.contains("encryption=none"));
    }

    #[test]
    fn output_is_accepted_by_the_uri_list_validator() {
        // The whole point: these lines have to survive the strict validator
        // that rejected the raw JSON.
        let out = to_uri_lines(LIVE).unwrap();
        let joined = out.lines.join("\n");
        assert!(super::super::fetch::validate("v2ray", &joined).is_ok());
    }

    #[test]
    fn every_line_exposes_a_host_the_filter_can_read() {
        // relabel drops hosts it cannot parse; a URI whose host is invisible
        // would bypass the unroutable/.tm filter.
        for line in to_uri_lines(LIVE).unwrap().lines {
            assert!(
                label::host_of(&line).is_some(),
                "no host parsed from {line}"
            );
        }
    }

    #[test]
    fn hysteria_v1_is_skipped_not_guessed() {
        let body = r#"[{"outbounds":[{"protocol":"hysteria","tag":"proxy",
          "settings":{"address":"1.2.3.4","port":443},
          "streamSettings":{"hysteriaSettings":{"version":1,"auth":"x"}}}],"remarks":"x"}]"#;
        let out = to_uri_lines(body).unwrap();
        assert!(out.lines.is_empty());
        assert!(out.skipped[0].contains("hysteria v1"), "{:?}", out.skipped);
    }

    #[test]
    fn vmess_is_skipped_with_a_reason() {
        let body = r#"[{"outbounds":[{"protocol":"vmess","tag":"proxy"}],"remarks":"x"}]"#;
        let out = to_uri_lines(body).unwrap();
        assert!(out.lines.is_empty());
        assert!(out.skipped[0].contains("vmess"), "{:?}", out.skipped);
    }

    #[test]
    fn a_profile_with_no_proxy_outbound_is_skipped() {
        let body = r#"[{"outbounds":[{"protocol":"freedom","tag":"direct"}],"remarks":"x"}]"#;
        let out = to_uri_lines(body).unwrap();
        assert!(out.lines.is_empty());
        assert!(out.skipped[0].contains("no proxy outbound"));
    }

    #[test]
    fn a_bare_object_is_accepted_as_one_profile() {
        let body = r#"{"outbounds":[{"protocol":"trojan","tag":"proxy",
          "settings":{"servers":[{"address":"h.example","port":443,"password":"pw"}]},
          "streamSettings":{"network":"tcp","security":"tls"}}],"remarks":"T"}"#;
        let out = to_uri_lines(body).unwrap();
        assert_eq!(out.lines.len(), 1);
        assert!(out.lines[0].starts_with("trojan://pw@h.example:443?"));
    }

    #[test]
    fn reality_keys_are_carried_over() {
        let body = r#"[{"outbounds":[{"protocol":"vless","tag":"proxy",
          "settings":{"vnext":[{"address":"1.2.3.4","port":443,
            "users":[{"id":"u","flow":"xtls-rprx-vision"}]}]},
          "streamSettings":{"network":"tcp","security":"reality",
            "realitySettings":{"serverName":"a.com","fingerprint":"chrome",
              "publicKey":"PBK","shortId":"SID","spiderX":"/"}}}],"remarks":"R"}]"#;
        let line = &to_uri_lines(body).unwrap().lines[0];
        assert!(line.contains("security=reality"));
        assert!(line.contains("pbk=PBK"));
        assert!(line.contains("sid=SID"));
        assert!(line.contains("flow=xtls-rprx-vision"));
    }

    #[test]
    fn an_ipv6_address_is_bracketed() {
        let body = r#"[{"outbounds":[{"protocol":"trojan","tag":"proxy",
          "settings":{"servers":[{"address":"2001:db8::1","port":443,"password":"pw"}]}}],
          "remarks":"6"}]"#;
        let line = &to_uri_lines(body).unwrap().lines[0];
        assert!(line.contains("@[2001:db8::1]:443"), "{line}");
    }

    #[test]
    fn a_ws_host_inside_headers_is_still_found() {
        let body = r#"[{"outbounds":[{"protocol":"vless","tag":"proxy",
          "settings":{"vnext":[{"address":"1.2.3.4","port":80,"users":[{"id":"u"}]}]},
          "streamSettings":{"network":"ws","wsSettings":{"path":"/x",
            "headers":{"Host":"cdn.example"}}}}],"remarks":"W"}]"#;
        let line = &to_uri_lines(body).unwrap().lines[0];
        assert!(line.contains("host=cdn.example"), "{line}");
    }

    #[test]
    fn an_awkward_password_cannot_break_out_of_the_userinfo() {
        let body = r#"[{"outbounds":[{"protocol":"trojan","tag":"proxy",
          "settings":{"servers":[{"address":"h.example","port":443,
            "password":"p@ss:w/rd?x#y"}]}}],"remarks":"T"}]"#;
        let line = &to_uri_lines(body).unwrap().lines[0];
        // The host the client resolves must still be h.example.
        assert_eq!(label::host_of(line).as_deref(), Some("h.example"));
        assert!(!line["trojan://".len()..].contains("p@ss"), "{line}");
        assert!(super::super::fetch::validate("v2ray", line).is_ok());
    }

    #[test]
    fn garbage_is_an_error_not_a_panic() {
        assert!(to_uri_lines("not json").is_err());
        assert!(to_uri_lines("\"a string\"").is_err());
    }
}

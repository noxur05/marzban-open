//! Google OAuth access tokens, cached until shortly before they expire.
//!
//! Two grants, one per store:
//!
//! * **Firestore** — a service account. An RS256-signed JWT is exchanged at the
//!   key's `token_uri` (the JWT-bearer grant) for a token with the `datastore`
//!   scope.
//! * **Drive** — the OAuth refresh token of a dedicated Google account. Service
//!   accounts have no Drive storage quota, so they cannot own the files.
//!
//! Nothing in here logs or returns key material or tokens in an error: every
//! error text is built from status codes and Google's own error message.

use std::time::{Duration, Instant};

use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use base64::Engine;
use ring::rand::SystemRandom;
use ring::signature::{RsaKeyPair, RSA_PKCS1_SHA256};
use serde_json::json;
use tokio::sync::Mutex;

use super::error::{google_error_message, Failure};

pub const FIRESTORE_SCOPE: &str = "https://www.googleapis.com/auth/datastore";
pub const DRIVE_TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const JWT_BEARER_GRANT: &str = "urn:ietf:params:oauth:grant-type:jwt-bearer";

/// Lifetime requested for the service-account assertion (Google's maximum).
const JWT_LIFETIME_SECS: u64 = 3600;

/// A cached token is dropped this long before Google says it expires.
const EXPIRY_MARGIN: Duration = Duration::from_secs(60);

/// Largest token-endpoint answer read.
const MAX_TOKEN_BODY: usize = 64 * 1024;

/// A parsed service-account key. Deliberately not `Debug`/`Clone`.
pub struct ServiceAccount {
    client_email: String,
    token_uri: String,
    key: RsaKeyPair,
}

impl ServiceAccount {
    /// Read and parse the JSON key file. Errors name the problem, never the
    /// content.
    pub fn load(path: &str) -> Result<ServiceAccount, String> {
        let raw = std::fs::read(path)
            .map_err(|e| format!("cannot read the service-account key file: {}", e.kind()))?;
        ServiceAccount::from_json(&raw)
    }

    pub fn from_json(raw: &[u8]) -> Result<ServiceAccount, String> {
        let v: serde_json::Value = serde_json::from_slice(raw)
            .map_err(|_| "the service-account key file is not JSON".to_string())?;
        let field = |k: &str| {
            v[k].as_str()
                .map(str::to_string)
                .filter(|s| !s.is_empty())
                .ok_or_else(|| format!("the service-account key has no `{k}`"))
        };
        let client_email = field("client_email")?;
        let token_uri = field("token_uri").unwrap_or_else(|_| DRIVE_TOKEN_URL.to_string());
        if !token_uri.starts_with("https://") {
            return Err("the service-account token_uri is not https".to_string());
        }
        let der = pem_to_der(&field("private_key")?)?;
        let key = RsaKeyPair::from_pkcs8(&der)
            .map_err(|e| format!("the service-account private key is unusable: {e}"))?;
        Ok(ServiceAccount {
            client_email,
            token_uri,
            key,
        })
    }

    pub fn client_email(&self) -> &str {
        &self.client_email
    }

    /// A signed JWT-bearer assertion: `b64u(header).b64u(claims).b64u(sig)`.
    pub fn assertion(&self, now_unix: u64) -> Result<String, String> {
        let input = signing_input(&self.client_email, &self.token_uri, now_unix);
        let mut sig = vec![0u8; self.key.public().modulus_len()];
        self.key
            .sign(
                &RSA_PKCS1_SHA256,
                &SystemRandom::new(),
                input.as_bytes(),
                &mut sig,
            )
            .map_err(|_| "RS256 signing failed".to_string())?;
        Ok(format!("{input}.{}", URL_SAFE_NO_PAD.encode(sig)))
    }
}

/// `b64u(header) "." b64u(claims)` — the bytes the RS256 signature covers.
pub fn signing_input(client_email: &str, token_uri: &str, now_unix: u64) -> String {
    let header = json!({"alg": "RS256", "typ": "JWT"});
    let claims = json!({
        "iss": client_email,
        "scope": FIRESTORE_SCOPE,
        "aud": token_uri,
        "iat": now_unix,
        "exp": now_unix + JWT_LIFETIME_SECS,
    });
    format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(header.to_string()),
        URL_SAFE_NO_PAD.encode(claims.to_string())
    )
}

/// PKCS#8 PEM (`-----BEGIN PRIVATE KEY-----`, what Google issues) → DER.
fn pem_to_der(pem: &str) -> Result<Vec<u8>, String> {
    const BEGIN: &str = "-----BEGIN PRIVATE KEY-----";
    const END: &str = "-----END PRIVATE KEY-----";
    let start = pem
        .find(BEGIN)
        .ok_or_else(|| "the service-account private key is not a PKCS#8 PEM".to_string())?;
    let rest = &pem[start + BEGIN.len()..];
    let end = rest
        .find(END)
        .ok_or_else(|| "the service-account private key PEM is truncated".to_string())?;
    let body: String = rest[..end].chars().filter(|c| !c.is_whitespace()).collect();
    STANDARD
        .decode(body)
        .map_err(|_| "the service-account private key PEM is not base64".to_string())
}

/// How a token is obtained.
pub enum Grant {
    ServiceAccount(Box<ServiceAccount>),
    RefreshToken {
        client_id: String,
        client_secret: String,
        refresh_token: String,
    },
}

struct Cached {
    token: String,
    valid_until: Instant,
}

/// One cached token source. Deliberately not `Debug`.
pub struct Authenticator {
    /// "firestore token" / "drive token", for error messages.
    label: &'static str,
    grant: Grant,
    cache: Mutex<Option<Cached>>,
}

impl Authenticator {
    pub fn new(label: &'static str, grant: Grant) -> Self {
        Authenticator {
            label,
            grant,
            cache: Mutex::new(None),
        }
    }

    /// A valid access token, from cache or freshly fetched.
    pub async fn token(&self, http: &reqwest::Client) -> Result<String, Failure> {
        let mut cache = self.cache.lock().await;
        if let Some(c) = cache.as_ref() {
            if Instant::now() < c.valid_until {
                return Ok(c.token.clone());
            }
        }
        let (token, lifetime) = self.fetch(http).await?;
        *cache = Some(Cached {
            token: token.clone(),
            valid_until: Instant::now() + usable_lifetime(lifetime),
        });
        Ok(token)
    }

    /// Forget the cached token (after a 401), so the next call fetches one.
    pub async fn invalidate(&self) {
        *self.cache.lock().await = None;
    }

    async fn fetch(&self, http: &reqwest::Client) -> Result<(String, Duration), Failure> {
        let request = match &self.grant {
            Grant::ServiceAccount(sa) => {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let assertion = sa
                    .assertion(now)
                    .map_err(|e| Failure::account(format!("{}: {e}", self.label)))?;
                http.post(&sa.token_uri)
                    .form(&[("grant_type", JWT_BEARER_GRANT), ("assertion", &assertion)])
            }
            Grant::RefreshToken {
                client_id,
                client_secret,
                refresh_token,
            } => http.post(DRIVE_TOKEN_URL).form(&[
                ("client_id", client_id.as_str()),
                ("client_secret", client_secret.as_str()),
                ("refresh_token", refresh_token.as_str()),
                ("grant_type", "refresh_token"),
            ]),
        };

        let mut resp = request
            .send()
            .await
            .map_err(|e| Failure::no_answer(self.label, &e))?;
        let status = resp.status().as_u16();
        let body = read_capped(&mut resp, MAX_TOKEN_BODY)
            .await
            .map_err(|e| Failure::no_answer(self.label, &e))?;

        if status != 200 {
            return Err(self.refused(status, &body));
        }
        parse_token_response(&body)
            .ok_or_else(|| Failure::account(format!("{}: unreadable token response", self.label)))
    }

    /// Any refusal from the token endpoint stops the whole loop: every call
    /// needs this token.
    fn refused(&self, status: u16, body: &[u8]) -> Failure {
        let detail = google_error_message(body);
        let invalid_grant = detail.starts_with("invalid_grant");
        let hint = match (&self.grant, invalid_grant) {
            (Grant::RefreshToken { .. }, true) => {
                " (the refresh token was revoked, or the OAuth consent screen is in Testing)"
            }
            (Grant::ServiceAccount(_), true) => {
                " (the service-account key was deleted, or the clock is off)"
            }
            _ => "",
        };
        let mut f = Failure::account(format!(
            "{} refused: HTTP {status} {}{hint}",
            self.label,
            super::error::clean(&super::error::redact_ids(&detail), 100)
        ));
        f.invalid_grant = invalid_grant;
        f
    }
}

/// `access_token` and `expires_in` from a token response.
pub fn parse_token_response(body: &[u8]) -> Option<(String, Duration)> {
    let v: serde_json::Value = serde_json::from_slice(body).ok()?;
    let token = v["access_token"].as_str().filter(|t| !t.is_empty())?;
    let expires_in = v["expires_in"].as_u64().unwrap_or(3600);
    Some((token.to_string(), Duration::from_secs(expires_in)))
}

/// How long a token is reused: Google's lifetime minus a minute, or half the
/// lifetime when that is shorter (never zero, never negative).
pub fn usable_lifetime(lifetime: Duration) -> Duration {
    if lifetime > EXPIRY_MARGIN * 2 {
        lifetime - EXPIRY_MARGIN
    } else {
        lifetime / 2
    }
}

/// Read a response body, refusing more than `max` bytes.
pub async fn read_capped(
    resp: &mut reqwest::Response,
    max: usize,
) -> Result<Vec<u8>, reqwest::Error> {
    let mut out = Vec::new();
    while let Some(chunk) = resp.chunk().await? {
        if out.len() + chunk.len() > max {
            // Truncated bodies are only ever used for error text; a token
            // response this large is unreadable anyway.
            let room = max.saturating_sub(out.len());
            out.extend_from_slice(&chunk[..room.min(chunk.len())]);
            break;
        }
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signing_input_has_the_jwt_bearer_claims() {
        let input = signing_input(
            "sa@p.iam.gserviceaccount.com",
            "https://oauth2.googleapis.com/token",
            1_700_000_000,
        );
        let (h, c) = input.split_once('.').unwrap();
        assert!(!input.contains('='), "base64url without padding");
        let header: serde_json::Value =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(h).unwrap()).unwrap();
        assert_eq!(header, json!({"alg": "RS256", "typ": "JWT"}));
        let claims: serde_json::Value =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(c).unwrap()).unwrap();
        assert_eq!(claims["iss"], "sa@p.iam.gserviceaccount.com");
        assert_eq!(claims["scope"], FIRESTORE_SCOPE);
        assert_eq!(claims["aud"], "https://oauth2.googleapis.com/token");
        assert_eq!(claims["iat"], 1_700_000_000_u64);
        assert_eq!(claims["exp"], 1_700_003_600_u64);
    }

    #[test]
    fn token_lifetime_keeps_a_minute_of_margin() {
        assert_eq!(
            usable_lifetime(Duration::from_secs(3599)),
            Duration::from_secs(3539)
        );
        assert_eq!(
            usable_lifetime(Duration::from_secs(100)),
            Duration::from_secs(50)
        );
        assert_eq!(
            usable_lifetime(Duration::from_secs(0)),
            Duration::from_secs(0)
        );
    }

    #[test]
    fn parses_token_responses() {
        let (t, d) = parse_token_response(
            br#"{"access_token":"ya29.x","expires_in":3599,"token_type":"Bearer"}"#,
        )
        .unwrap();
        assert_eq!(t, "ya29.x");
        assert_eq!(d, Duration::from_secs(3599));
        assert!(parse_token_response(br#"{"access_token":""}"#).is_none());
        assert!(parse_token_response(b"nope").is_none());
    }

    #[test]
    fn bad_keys_are_rejected_without_echoing_them() {
        let sa = |pk: &str| {
            json!({"client_email": "a@b", "token_uri": "https://oauth2.googleapis.com/token", "private_key": pk})
                .to_string()
        };
        let secretish =
            "-----BEGIN PRIVATE KEY-----\nTOPSECRETMATERIAL\n-----END PRIVATE KEY-----\n";
        let err = ServiceAccount::from_json(sa(secretish).as_bytes())
            .err()
            .unwrap();
        assert!(!err.contains("TOPSECRET"), "{err}");
        let err = ServiceAccount::from_json(sa("not a pem").as_bytes())
            .err()
            .unwrap();
        assert!(err.contains("PKCS#8"), "{err}");
        let err = ServiceAccount::from_json(b"{}").err().unwrap();
        assert!(err.contains("client_email"), "{err}");
        let insecure =
            json!({"client_email": "a@b", "token_uri": "http://x", "private_key": secretish})
                .to_string();
        assert!(ServiceAccount::from_json(insecure.as_bytes()).is_err());
    }

    /// Signs with a throwaway key made by the `openssl` binary and verifies the
    /// signature with ring. Skipped where openssl is not installed; the live
    /// test covers the same path against Google.
    #[test]
    fn assertion_is_a_valid_rs256_signature() {
        let out = std::process::Command::new("openssl")
            .args([
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ])
            .output();
        let pem = match out {
            Ok(o) if o.status.success() => String::from_utf8(o.stdout).unwrap(),
            _ => {
                eprintln!("skipping: openssl not available");
                return;
            }
        };
        let raw = json!({
            "client_email": "sa@p.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
            "private_key": pem,
        })
        .to_string();
        let sa = ServiceAccount::from_json(raw.as_bytes()).unwrap();
        let jwt = sa.assertion(1_700_000_000).unwrap();
        let (input, sig) = jwt.rsplit_once('.').unwrap();
        assert_eq!(
            input,
            signing_input(
                "sa@p.iam.gserviceaccount.com",
                "https://oauth2.googleapis.com/token",
                1_700_000_000
            )
        );
        let sig = URL_SAFE_NO_PAD.decode(sig).unwrap();
        let public = ring::signature::UnparsedPublicKey::new(
            &ring::signature::RSA_PKCS1_2048_8192_SHA256,
            sa.key.public().as_ref(),
        );
        public.verify(input.as_bytes(), &sig).unwrap();
    }
}

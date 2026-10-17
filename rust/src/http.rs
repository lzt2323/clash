use anyhow::{Context, Result};
use std::time::Duration;
use url::Url;

/// External downloads follow shell HTTP(S) proxies; controller traffic never does.
pub fn external_agent(url: &Url, timeout: Duration) -> Result<ureq::Agent> {
    let mut builder = ureq::AgentBuilder::new()
        .timeout(timeout)
        .try_proxy_from_env(false);
    let bypass = std::env::var("no_proxy")
        .or_else(|_| std::env::var("NO_PROXY"))
        .unwrap_or_default();
    if !bypasses(url, &bypass) {
        let key = format!("{}_proxy", url.scheme());
        let proxy = std::env::var(&key)
            .or_else(|_| std::env::var(key.to_uppercase()))
            .unwrap_or_default();
        if !proxy.is_empty() {
            builder = builder.proxy(
                ureq::Proxy::new(proxy).context("当前终端代理地址无效，请检查代理环境变量。")?,
            );
        }
    }
    Ok(builder.build())
}

fn bypasses(url: &Url, list: &str) -> bool {
    let host = url.host_str().unwrap_or("").to_lowercase();
    let authority = format!("{host}:{}", url.port_or_known_default().unwrap_or(0));
    list.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .any(|entry| {
            let entry = entry.to_lowercase();
            let domain = entry.trim_start_matches('.');
            entry == "*"
                || entry == authority
                || host == domain
                || host.ends_with(&format!(".{domain}"))
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn no_proxy_matches_hosts_domains_and_ports_without_matching_other_domains() {
        assert!(bypasses(
            &Url::parse("http://127.0.0.1:1234").unwrap(),
            "localhost,127.0.0.1"
        ));
        assert!(bypasses(
            &Url::parse("https://sub.example.com").unwrap(),
            ".example.com"
        ));
        assert!(!bypasses(
            &Url::parse("https://notexample.com").unwrap(),
            "example.com"
        ));
        assert!(bypasses(
            &Url::parse("https://example.com:8443").unwrap(),
            "example.com:8443"
        ));
        assert!(!bypasses(
            &Url::parse("https://example.com:443").unwrap(),
            "example.com:8443"
        ));
    }
}

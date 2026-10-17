//! Shared display text and redacted worker responses for CLI, menus and maintenance.
use crate::storage::Cancelled;
use anyhow::Result;
use regex::Regex;
use serde_json::{json, Value};
use std::sync::LazyLock;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

pub const MODES: [(&str, &str, &str); 3] = [
    ("rule", "规则", "私网直连，其他走代理。"),
    ("global", "全局", "接入核心的流量全部交给所选代理。"),
    ("direct", "直连", "接入核心的流量直接访问。"),
];
static ANSI: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\x1b\[[0-?]*[ -/]*[@-~]").unwrap());
static URLS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?i)https?://[^\s<>"']+"#).unwrap());
static CONTROLS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\p{C}").unwrap());

pub fn clean(value: &str) -> String {
    CONTROLS
        .replace_all(
            &URLS.replace_all(&ANSI.replace_all(value, ""), "[地址已隐藏]"),
            "",
        )
        .into_owned()
}
pub fn clip(value: &str, width: usize) -> String {
    let text = clean(value);
    if text.width() <= width {
        return text;
    }
    let suffix = if width >= 3 { "..." } else { "" };
    let mut used = 0;
    let mut output = String::new();
    for c in text.chars() {
        let size = c.width().unwrap_or(0);
        if used + size > width - suffix.len() {
            break;
        }
        used += size;
        output.push(c);
    }
    output.push_str(suffix);
    output
}
pub fn node_label(name: &str) -> String {
    if name == "AUTO" {
        "自动选择".into()
    } else {
        clean(name)
    }
}
pub fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}
pub fn active(state: &Value) -> Option<&Value> {
    state["subscriptions"]
        .as_array()?
        .iter()
        .find(|s| s["id"] == state["active"])
}
pub fn delay_label(value: Option<&Value>) -> String {
    match value {
        Some(v) if v.as_f64().is_some_and(|d| d > 0.0) => format!("{v} ms"),
        Some(Value::String(s)) => clean(s),
        Some(_) => "失败".into(),
        None => "未测".into(),
    }
}

pub fn mode_label(mode: &str) -> &str {
    MODES
        .iter()
        .find(|(id, _, _)| *id == mode)
        .map(|(_, name, _)| *name)
        .unwrap_or(mode)
}
pub fn envelope(result: Result<Value>) -> Value {
    match result {
        Ok(result) => json!({"ok":true,"result":result}),
        Err(error) if error.downcast_ref::<Cancelled>().is_some() => {
            json!({"ok":false,"error":"已取消，原设置未修改。","cancelled":true})
        }
        Err(error) => json!({"ok":false,"error":safe_error(&error)}),
    }
}
pub fn safe_error(error: &anyhow::Error) -> String {
    let message = error.to_string();
    if message.contains("http://") || message.contains("https://") || message.is_ascii() {
        "操作失败，请检查安装、文件权限及核心状态。".into()
    } else {
        clean(&message)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn url_and_terminal_controls_are_redacted() {
        assert_eq!(
            clean("\x1b[31mhello https://host/?token=PRIVATE\x1b[0m\u{202e}"),
            "hello [地址已隐藏]"
        );
        assert!(clip("香港Tokyo", 5).width() <= 5);
    }
    #[test]
    fn worker_errors_preserve_cancellation_and_hide_details() {
        let cancelled = envelope(Err(Cancelled.into()));
        assert_eq!(cancelled["cancelled"], true);
        assert_eq!(cancelled["ok"], false);
        let error = envelope(Err(anyhow::anyhow!("下载失败 https://host/?token=PRIVATE")));
        assert!(!error.to_string().contains("PRIVATE"));
        assert_eq!(error["ok"], false);
        assert_eq!(envelope(Ok(json!("完成")))["result"], "完成");
    }
}

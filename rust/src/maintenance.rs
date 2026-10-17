use crate::manager::Manager;
use crate::storage::{atomic, digest, private_dir, Lock};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub version: u32,
    pub update_interval_secs: u64,
    pub backup_before_update: bool,
    pub backup_keep: usize,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            version: 1,
            update_interval_secs: 0,
            backup_before_update: true,
            backup_keep: 10,
        }
    }
}
pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn directory(manager: &Manager) -> PathBuf {
    manager.root.join("runtime/mvp")
}
pub fn settings(manager: &Manager) -> Result<Settings> {
    let path = directory(manager).join("settings.json");
    let settings: Settings = if path.exists() {
        serde_json::from_slice(&fs::read(path)?)
            .context("自动更新 / 备份设置损坏，请检查 settings.json。")?
    } else {
        Settings::default()
    };
    if settings.version != 1
        || !(1..=100).contains(&settings.backup_keep)
        || (settings.update_interval_secs != 0
            && !(300..=30 * 86400).contains(&settings.update_interval_secs))
    {
        bail!("自动更新 / 备份设置无效，请检查 settings.json。")
    }
    Ok(settings)
}
fn save(manager: &Manager, settings: &Settings) -> Result<()> {
    atomic(
        &directory(manager).join("settings.json"),
        serde_json::to_vec_pretty(settings)?,
    )
}
pub fn status(manager: &Manager) -> Result<Value> {
    let path = directory(manager).join("update-status.json");
    let last: Value = if path.exists() {
        serde_json::from_slice(&fs::read(path)?).context("自动更新状态记录损坏。")?
    } else {
        json!({})
    };
    Ok(
        json!({"settings": settings(manager)?, "last_run": last, "backups": crate::backup::list(manager)?}),
    )
}
pub fn parse_interval(value: &str) -> Result<u64> {
    if matches!(value, "off" | "0" | "关闭") {
        return Ok(0);
    }
    let (digits, multiplier) = match value.as_bytes().last() {
        Some(b'm') => (&value[..value.len() - 1], 60),
        Some(b'h') => (&value[..value.len() - 1], 3600),
        Some(b'd') => (&value[..value.len() - 1], 86400),
        _ => (value, 3600),
    };
    let seconds = digits
        .parse::<u64>()
        .ok()
        .and_then(|n| n.checked_mul(multiplier))
        .context("请输入更新间隔，例如 30m、6h、1d；off 关闭。")?;
    if !(300..=30 * 86400).contains(&seconds) {
        bail!("更新间隔须在 5 分钟到 30 天之间。")
    }
    Ok(seconds)
}
fn markers(manager: &Manager) -> (String, String) {
    let id = &digest(manager.root.to_string_lossy().as_bytes())[..16];
    (
        format!("# >>> clash-linux updates {id} >>>"),
        format!("# <<< clash-linux updates {id} <<<"),
    )
}
fn remove_block(contents: &str, begin: &str, end: &str) -> Result<String> {
    let mut out = String::new();
    let mut inside = false;
    for line in contents.lines() {
        if line == begin {
            if inside {
                bail!("定时任务标记损坏。")
            }
            inside = true;
            continue;
        }
        if line == end {
            if !inside {
                bail!("定时任务标记损坏。")
            }
            inside = false;
            continue;
        }
        if !inside {
            out.push_str(line);
            out.push('\n');
        }
    }
    if inside {
        bail!("定时任务标记不完整，请先修复 crontab。")
    }
    Ok(out)
}
fn read_crontab() -> Result<String> {
    let output = Command::new("crontab")
        .arg("-l")
        .env("LC_ALL", "C")
        .output()
        .context("定时更新需要系统 crontab 命令；手动更新和备份仍可使用。")?;
    if output.status.success() {
        return String::from_utf8(output.stdout).context("定时任务内容编码无效。");
    }
    if String::from_utf8_lossy(&output.stderr)
        .to_lowercase()
        .contains("no crontab for")
    {
        return Ok(String::new());
    }
    bail!("无法读取当前用户的 crontab，请检查系统定时服务和权限。")
}
fn write_crontab(contents: &str) -> Result<()> {
    let mut child = Command::new("crontab")
        .arg("-")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("无法写入 crontab。")?;
    let written = child
        .stdin
        .take()
        .context("定时任务输入不可用。")?
        .write_all(contents.as_bytes());
    let status = child.wait()?;
    written?;
    if !status.success() {
        bail!("系统拒绝保存定时任务，原更新设置未修改。")
    }
    Ok(())
}
pub fn cron_text(manager: &Manager, original: &str, enabled: bool) -> Result<String> {
    let (begin, end) = markers(manager);
    let mut contents = remove_block(original, &begin, &end)?;
    if enabled {
        if manager
            .root
            .to_string_lossy()
            .chars()
            .any(|c| c.is_control())
        {
            bail!("安装路径不能包含控制字符。")
        }
        let quote = crate::shell::quote;
        let command = format!("CLASH_HTTP_PROXY={} CLASH_SOCKS_PROXY={} MIHOMO_API_URL={} {} --root {} subscription update-due >/dev/null 2>&1",
            quote(manager.http_url.as_str()),quote(manager.socks_url.as_str()),quote(manager.api_url.as_str()),
            quote(&manager.root.join("clash").to_string_lossy()),quote(&manager.root.to_string_lossy()));
        contents.push_str(&format!(
            "{begin}\n*/5 * * * * {}\n{end}\n",
            command.replace('%', "\\%")
        ));
    }
    Ok(contents)
}
pub fn configure(manager: &Manager, value: &str) -> Result<Value> {
    let seconds = parse_interval(value)?;
    let mut updated = settings(manager)?;
    let previous = read_crontab()?;
    let next = cron_text(manager, &previous, seconds != 0)?;
    write_crontab(&next)?;
    updated.update_interval_secs = seconds;
    if let Err(error) = save(manager, &updated) {
        write_crontab(&previous).context("保存设置失败且定时任务回退未完成，请检查 crontab。")?;
        return Err(error);
    }
    Ok(json!(if seconds == 0 {
        "订阅自动更新已关闭。".into()
    } else {
        format!(
            "已开启订阅自动更新，间隔 {} 分钟；系统每 5 分钟检查到期任务。",
            seconds / 60
        )
    }))
}
pub fn remove_schedule(manager: &Manager) -> Result<()> {
    let mut settings = settings(manager)?;
    if settings.update_interval_secs == 0 {
        return Ok(());
    }
    let previous = read_crontab()?;
    write_crontab(&cron_text(manager, &previous, false)?)?;
    settings.update_interval_secs = 0;
    if let Err(error) = save(manager, &settings) {
        write_crontab(&previous)?;
        return Err(error);
    }
    Ok(())
}
pub fn configure_backup(manager: &Manager, key: &str, value: &str) -> Result<Value> {
    let mut settings = settings(manager)?;
    if key == "backup_auto_set" {
        settings.backup_before_update = match value {
            "on" => true,
            "off" => false,
            _ => bail!("请选择 on 或 off。"),
        };
    } else {
        settings.backup_keep = value
            .parse()
            .ok()
            .filter(|n| (1..=100).contains(n))
            .context("备份保留份数须为 1–100。")?;
    }
    save(manager, &settings)?;
    Ok(json!("备份设置已保存。"))
}
pub fn update(manager: &Manager, due_only: bool) -> Result<Value> {
    private_dir(&directory(manager))?;
    let _lock = Lock::acquire(&directory(manager).join("update.lock"), true)?;
    let settings = settings(manager)?;
    let status_path = directory(manager).join("update-status.json");
    let previous: Value = if status_path.exists() {
        serde_json::from_slice(&fs::read(&status_path)?).context("自动更新状态记录损坏。")?
    } else {
        json!({})
    };
    let now = now();
    if due_only
        && (settings.update_interval_secs == 0
            || now.saturating_sub(previous["last_attempt"].as_u64().unwrap_or(0))
                < settings.update_interval_secs)
    {
        return Ok(json!("尚未到更新时间。"));
    }
    let state = manager.load()?;
    if state.subscriptions.is_empty() {
        return Ok(json!("尚无订阅，已跳过更新。"));
    }
    manager.cancel.checkpoint()?;
    let mut report = json!({"last_attempt":now,"completed":false,"results":[]});
    atomic(&status_path, serde_json::to_vec_pretty(&report)?)?;
    if settings.backup_before_update {
        match manager.perform("backup_create", &[]) {
            Ok(name) => report["backup"] = name,
            Err(error) => {
                report["error"] = json!(crate::output::safe_error(&error));
                atomic(&status_path, serde_json::to_vec_pretty(&report)?)?;
                return Err(error);
            }
        }
    }
    let mut results = Vec::new();
    let mut failed = 0;
    for item in state.subscriptions {
        manager.cancel.checkpoint()?;
        let result = manager.perform("update", &[json!(item.id)]);
        results.push(match result {
            Ok(_) => json!({"id":item.id,"name":crate::output::clean(&item.name),"ok":true}),
            Err(error) => { failed += 1; json!({"id":item.id,"name":crate::output::clean(&item.name),"ok":false,"error":crate::output::safe_error(&error)}) }
        });
        report["results"] = json!(results);
        atomic(&status_path, serde_json::to_vec_pretty(&report)?)?;
    }
    report["completed"] = json!(true);
    report["failed"] = json!(failed);
    report["message"] = json!(format!(
        "订阅更新完成：成功 {}，失败 {}。",
        results.len() - failed,
        failed
    ));
    atomic(&status_path, serde_json::to_vec_pretty(&report)?)?;
    if failed > 0 {
        bail!("部分订阅更新失败，原数据已保留；请查看更新状态。")
    }
    Ok(report["message"].clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn interval_limits_and_units() {
        assert_eq!(parse_interval("6h").unwrap(), 21600);
        assert_eq!(parse_interval("off").unwrap(), 0);
        for value in ["1m", "0h", "99999999999999999999d", "31d", "bad"] {
            assert!(parse_interval(value).is_err());
        }
    }
    #[test]
    fn cron_updates_are_owned_idempotent_and_preserve_other_jobs() {
        let root = tempfile::tempdir().unwrap();
        let manager = Manager::new(root.path().into()).unwrap();
        let original = "# user's job\n0 1 * * * /bin/true\n";
        let first = cron_text(&manager, original, true).unwrap();
        assert_eq!(cron_text(&manager, &first, true).unwrap(), first);
        assert_eq!(cron_text(&manager, &first, false).unwrap(), original);
        assert!(first.contains("subscription update-due"));
    }
}

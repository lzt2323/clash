use crate::manager::{Manager, State};
use crate::storage::{atomic, digest, private_dir, random_hex};
use anyhow::{bail, Context, Result};
use flate2::{read::GzDecoder, write::GzEncoder, Compression};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

const MAX_SIZE: u64 = 128 * 1024 * 1024;
fn directory(manager: &Manager) -> PathBuf {
    manager.root.join("runtime/mvp/backups")
}
fn valid_name(name: &str) -> bool {
    name.starts_with("backup-")
        && name.ends_with(".tar.gz")
        && name.len() < 100
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '.'))
}
fn cache(root: &Path, relative: &str) -> Result<PathBuf> {
    let path = Path::new(relative);
    if path.is_absolute()
        || path
            .components()
            .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
    {
        bail!("订阅缓存路径无效，备份未修改。")
    }
    let base = fs::canonicalize(root.join("conf"))?;
    let path = fs::canonicalize(base.join(path)).context("订阅缓存缺失，无法备份或恢复。")?;
    if !path.starts_with(base.join("mvp-providers")) || !path.is_file() {
        bail!("订阅缓存路径越界。")
    }
    let data = fs::read(&path)?;
    if path.file_stem().and_then(|s| s.to_str()) != Some(&digest(&data)) {
        bail!("订阅缓存损坏，无法备份或恢复。")
    }
    Ok(path)
}
pub fn list(manager: &Manager) -> Result<Vec<String>> {
    let path = directory(manager);
    if !path.exists() {
        return Ok(vec![]);
    }
    let mut names = Vec::new();
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if valid_name(&name) && entry.file_type()?.is_file() {
            names.push(name);
        }
    }
    names.sort();
    names.reverse();
    Ok(names)
}
pub fn create(manager: &Manager) -> Result<String> {
    let state = manager.load()?;
    let mut files = vec![(
        "runtime/mvp/state.json".to_owned(),
        serde_json::to_vec_pretty(&state)?,
    )];
    let mut seen = HashSet::new();
    for item in &state.subscriptions {
        let name = format!("conf/{}", item.cache.trim_start_matches("./"));
        if seen.insert(name.clone()) {
            files.push((name, fs::read(cache(&manager.root, &item.cache)?)?));
        }
    }
    if manager.config_path().is_file() {
        files.push(("conf/config.yaml".into(), fs::read(manager.config_path())?));
    }
    if files.iter().map(|(_, data)| data.len() as u64).sum::<u64>() > MAX_SIZE {
        bail!("配置数据超过 128 MB，无法备份。")
    }
    let gzip = GzEncoder::new(Vec::new(), Compression::default());
    let mut archive = tar::Builder::new(gzip);
    for (name, data) in files {
        let mut header = tar::Header::new_gnu();
        header.set_size(data.len() as u64);
        header.set_mode(0o600);
        header.set_cksum();
        archive.append_data(&mut header, name, data.as_slice())?;
    }
    let data = archive.into_inner()?.finish()?;
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let name = format!("backup-{timestamp:020}-{}.tar.gz", random_hex(4)?);
    private_dir(&directory(manager))?;
    atomic(&directory(manager).join(&name), data)?;
    let keep = crate::maintenance::settings(manager)?.backup_keep;
    for old in list(manager)?.into_iter().skip(keep) {
        fs::remove_file(directory(manager).join(old))?;
    }
    Ok(name)
}
pub fn restore(manager: &Manager, name: &str) -> Result<Value> {
    if !valid_name(name) {
        bail!("备份名称无效，请使用备份列表中的名称。")
    }
    let path = directory(manager).join(name);
    let meta = path.symlink_metadata().context("备份不存在。")?;
    if !meta.file_type().is_file() || meta.len() > MAX_SIZE {
        bail!("备份文件类型或大小无效。")
    }
    let stage = tempfile::tempdir()?;
    let mut tar = tar::Archive::new(GzDecoder::new(fs::File::open(path)?));
    let mut paths = HashSet::new();
    let mut size = 0u64;
    for entry in tar.entries()? {
        let entry = entry?;
        let path = entry.path()?.into_owned();
        let name = path.to_string_lossy();
        if !entry.header().entry_type().is_file()
            || path.is_absolute()
            || path
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
            || !paths.insert(path.clone())
            || !(name == "runtime/mvp/state.json"
                || name == "conf/config.yaml"
                || name.starts_with("conf/mvp-providers/"))
        {
            bail!("备份包含链接、重复或越界文件，未恢复。")
        }
        size = size.checked_add(entry.size()).context("备份大小无效。")?;
        if size > MAX_SIZE {
            bail!("备份解压内容超过 128 MB。")
        }
        let mut data = Vec::new();
        entry.take(MAX_SIZE + 1).read_to_end(&mut data)?;
        atomic(&stage.path().join(&path), data)?;
    }
    if !paths.contains(Path::new("runtime/mvp/state.json")) {
        bail!("备份缺少订阅记录。")
    }
    let new: State = Manager::new(stage.path().into())?.load()?;
    let mut providers = Vec::new();
    for item in &new.subscriptions {
        providers.push((
            item.cache.clone(),
            fs::read(cache(stage.path(), &item.cache)?)?,
        ));
    }
    manager.cancel.checkpoint()?;
    // Always retain a private snapshot before replacing the current settings.
    let before = create(manager)?;
    for (relative, data) in providers {
        atomic(&manager.root.join("conf").join(relative), data)?;
    }
    manager.apply(&manager.load()?, &new)?;
    Ok(json!(format!("配置已恢复。恢复前的备份：{before}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_generated_backup_names_are_accepted() {
        assert!(valid_name("backup-123-aabb.tar.gz"));
        for name in [
            "../backup-a.tar.gz",
            "/tmp/backup.tar.gz",
            "backup-x/one.tar.gz",
        ] {
            assert!(!valid_name(name));
        }
    }
    #[test]
    fn empty_configuration_roundtrips_without_credentials_in_listing() {
        let root = tempfile::tempdir().unwrap();
        let manager = Manager::new(root.path().into()).unwrap();
        let name = create(&manager).unwrap();
        assert_eq!(list(&manager).unwrap(), vec![name.clone()]);
        restore(&manager, &name).unwrap();
        assert!(manager.load().unwrap().subscriptions.is_empty());
        assert!(!manager.config_path().exists());
    }
}

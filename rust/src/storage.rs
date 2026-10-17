use anyhow::{bail, Context, Result};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

pub fn private_dir(path: &Path) -> Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
        .context("无法创建数据目录，请检查文件权限。")?;
    Ok(())
}

pub fn atomic(path: &Path, data: impl AsRef<[u8]>) -> Result<()> {
    let parent = path.parent().context("文件路径无效。")?;
    private_dir(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.as_file()
        .set_permissions(fs::Permissions::from_mode(0o600))?;
    file.write_all(data.as_ref())?;
    file.as_file().sync_all()?;
    file.persist(path)
        .map_err(|_| anyhow::anyhow!("保存文件失败，请检查文件权限。"))?;
    File::open(parent)?.sync_all()?;
    Ok(())
}

pub fn remove_file(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => bail!("无法删除文件，请检查文件权限。"),
    }
}

pub fn digest(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}

pub fn random_hex(bytes: usize) -> Result<String> {
    let mut data = vec![0; bytes];
    File::open("/dev/urandom")?.read_exact(&mut data)?;
    Ok(data.iter().map(|byte| format!("{byte:02x}")).collect())
}

pub fn sibling(root: &Path, suffix: &str) -> PathBuf {
    root.with_file_name(format!(
        ".{}{suffix}",
        root.file_name().unwrap().to_string_lossy()
    ))
}

/// CLOEXEC ensures the long-lived proxy core never retains a management lock.
pub struct Lock {
    _file: File,
}

impl Lock {
    pub fn acquire(path: &Path, exclusive: bool) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .context("无法创建操作锁，请检查安装目录权限。")?;
        if !file.metadata()?.is_file() {
            bail!("操作锁不是普通文件，请检查安装目录。")
        }
        let mode = if exclusive {
            libc::LOCK_EX
        } else {
            libc::LOCK_SH
        };
        // The descriptor belongs to this File for the entire lock lifetime.
        if unsafe { libc::flock(file.as_raw_fd(), mode | libc::LOCK_NB) } != 0 {
            bail!("另一个操作、菜单、安装或升级正在进行，请稍后重试。")
        }
        Ok(Self { _file: file })
    }

    pub fn operation(root: &Path, exclusive: bool) -> Result<Self> {
        let lock = Self::acquire(&sibling(root, ".operation.lock"), exclusive)?;
        if !exclusive && sibling(root, ".upgrade.json").exists() {
            bail!("存在未完成的升级，请先运行 clash recover。")
        }
        Ok(lock)
    }
}

#[derive(Debug)]
pub struct Cancelled;
impl std::fmt::Display for Cancelled {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "已取消，原设置未修改。")
    }
}
impl std::error::Error for Cancelled {}

#[derive(Clone, Default)]
pub struct Cancellation(pub Arc<AtomicBool>);
impl Cancellation {
    pub fn install(&self) -> Result<()> {
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            signal_hook::flag::register(signal, self.0.clone())?;
        }
        Ok(())
    }
    pub fn checkpoint(&self) -> Result<()> {
        if self.0.load(Ordering::Relaxed) {
            return Err(Cancelled.into());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn atomic_replacement_is_private_and_complete() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("nested/state.json");
        atomic(&path, b"old").unwrap();
        atomic(&path, b"new").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"new");
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    #[test]
    fn exclusive_lock_conflicts_with_live_shared_lock() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("lock");
        let _shared = Lock::acquire(&path, false).unwrap();
        assert!(Lock::acquire(&path, true).is_err());
        assert!(Lock::acquire(&path, false).is_ok());
    }
    #[test]
    fn symlink_lock_is_rejected() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("target");
        fs::write(&target, "unchanged").unwrap();
        let link = root.path().join("lock");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(Lock::acquire(&link, true).is_err());
        assert_eq!(fs::read_to_string(target).unwrap(), "unchanged");
    }
}

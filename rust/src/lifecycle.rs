use crate::storage::{atomic, remove_file};
use anyhow::{bail, Context, Result};
use std::fs::{self, OpenOptions};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub struct ChildGuard(pub Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
        }
        let _ = self.0.wait();
    }
}

pub fn command_output(command: &mut Command, timeout: Duration) -> Result<Output> {
    // File-backed output keeps a verbose core from blocking on a full pipe.
    let out = tempfile::tempfile()?;
    let err = tempfile::tempfile()?;
    let mut child = ChildGuard(
        command
            .stdin(Stdio::null())
            .stdout(out.try_clone()?)
            .stderr(err.try_clone()?)
            .spawn()
            .context("无法执行本地程序，请检查安装。")?,
    );
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.0.try_wait()? {
            break status;
        }
        if started.elapsed() >= timeout {
            bail!("本地程序操作超时，请检查核心状态。")
        }
        thread::sleep(Duration::from_millis(20));
    };
    use std::io::{Read, Seek, SeekFrom};
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut out = out;
    let mut err = err;
    out.seek(SeekFrom::Start(0))?;
    err.seek(SeekFrom::Start(0))?;
    out.take(1024 * 1024).read_to_end(&mut stdout)?;
    err.take(1024 * 1024).read_to_end(&mut stderr)?;
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

pub struct Core {
    pub root: PathBuf,
    pub binary: PathBuf,
}

impl Core {
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.into(),
            binary: std::env::var_os("MIHOMO_BINARY")
                .map(PathBuf::from)
                .unwrap_or_else(|| root.join("bin/mihomo")),
        }
    }
    fn pid_path(&self) -> PathBuf {
        self.root.join("runtime/mihomo.pid")
    }
    pub fn require_binary(&self) -> Result<&Path> {
        if !self.binary.is_file() {
            bail!("缺少 Mihomo 内核，请先运行 clash install。")
        }
        Ok(&self.binary)
    }
    fn pid(&self) -> Option<i32> {
        let pid: i32 = fs::read_to_string(self.pid_path())
            .ok()?
            .trim()
            .parse()
            .ok()?;
        (pid > 1).then_some(pid)
    }
    fn owns_pid(&self, pid: i32) -> bool {
        #[cfg(target_os = "linux")]
        {
            let expected = fs::canonicalize(&self.binary).ok();
            let actual = fs::read_link(format!("/proc/{pid}/exe")).ok();
            expected.is_some() && expected == actual
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = pid;
            false
        }
    }
    pub fn running(&self) -> bool {
        self.pid().is_some_and(|pid| self.owns_pid(pid))
    }
    pub fn validate(&self, path: &Path) -> Result<()> {
        let output = command_output(
            Command::new(self.require_binary()?)
                .arg("-t")
                .arg("-d")
                .arg(self.root.join("conf"))
                .arg("-f")
                .arg(path),
            Duration::from_secs(15),
        )?;
        if !output.status.success() {
            bail!("配置校验失败，原配置未修改。")
        }
        Ok(())
    }
    pub fn start(&self, healthy: impl Fn() -> bool) -> Result<()> {
        #[cfg(not(target_os = "linux"))]
        {
            let _ = &healthy;
            bail!("核心生命周期仅支持 Linux；当前平台可构建和执行单元测试。");
        }
        #[allow(unreachable_code)]
        {
            self.require_binary()?;
            if self.running() {
                bail!("核心已在运行，请先检查状态。")
            }
            self.validate(&self.root.join("conf/config.yaml"))?;
            fs::create_dir_all(self.root.join("logs"))?;
            let logfile = std::env::var_os("MIHOMO_LOG")
                .map(PathBuf::from)
                .unwrap_or_else(|| self.root.join("logs/mihomo.log"));
            let log = OpenOptions::new().create(true).append(true).open(logfile)?;
            let mut command = Command::new(&self.binary);
            command
                .arg("-d")
                .arg(self.root.join("conf"))
                .arg("-f")
                .arg(self.root.join("conf/config.yaml"))
                .stdin(Stdio::null())
                .stdout(log.try_clone()?)
                .stderr(log);
            // A detached core survives SSH exit and cannot inherit management locks.
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
            let mut child = command
                .spawn()
                .context("核心启动失败，请检查安装和运行日志。")?;
            let result = (|| -> Result<()> {
                atomic(&self.pid_path(), format!("{}\n", child.id()))?;
                let deadline = Instant::now() + Duration::from_secs(15);
                while Instant::now() < deadline {
                    if child.try_wait()?.is_some() {
                        bail!("核心启动失败，请检查端口占用或运行日志。")
                    }
                    if self.running() && healthy() {
                        return Ok(());
                    }
                    thread::sleep(Duration::from_millis(100));
                }
                bail!("核心启动超时，请检查运行日志。")
            })();
            if result.is_err() {
                let _ = child.kill();
                let _ = child.wait();
                let _ = remove_file(&self.pid_path());
            }
            result
        }
    }
    pub fn stop(&self) -> Result<()> {
        let Some(pid) = self.pid() else {
            return Ok(());
        };
        if !self.owns_pid(pid) {
            remove_file(&self.pid_path())?;
            bail!("已移除过期 PID；拒绝停止不属于当前核心的进程。")
        }
        // Check identity again before each signal, including after the grace period.
        if unsafe { libc::kill(pid, libc::SIGTERM) } != 0 {
            bail!("核心停止失败，请检查运行状态。")
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        while self.owns_pid(pid) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(50));
        }
        if self.owns_pid(pid) && unsafe { libc::kill(pid, libc::SIGKILL) } != 0 {
            bail!("无法停止核心，请检查运行状态。")
        }
        // Reap when this process started the core itself (integration / long-lived use).
        unsafe {
            libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG);
        }
        remove_file(&self.pid_path())
    }
}

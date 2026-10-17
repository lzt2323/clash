use crate::lifecycle::command_output;
use crate::manager::Manager;
use crate::shell;
use crate::storage::{atomic, digest, private_dir, remove_file, sibling, Lock};
use anyhow::{bail, Context, Result};
use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::time::Duration;

const CORE_VERSION: &str = "v1.19.29";
const MARKER: &str = ".clash-rust-install.json";
const DATA_MARKER: &str = ".clash-rust-data.json";
const MAX_ARCHIVE: u64 = 128 * 1024 * 1024;

pub fn print_env(manager: &Manager) {
    for key in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"] {
        println!(
            "export {key}={}",
            shell::quote(manager.http_url.as_str().trim_end_matches('/'))
        );
    }
    for key in ["all_proxy", "ALL_PROXY"] {
        println!(
            "export {key}={}",
            shell::quote(manager.socks_url.as_str().trim_end_matches('/'))
        );
    }
    let bypass = std::env::var("CLASH_NO_PROXY").unwrap_or("127.0.0.1,localhost,::1".into());
    for key in ["no_proxy", "NO_PROXY"] {
        println!("export {key}={}", shell::quote(&bypass));
    }
}
pub fn proxy_environment(manager: &Manager, command: &mut Command) {
    for key in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"] {
        command.env(key, manager.http_url.as_str().trim_end_matches('/'));
    }
    for key in ["all_proxy", "ALL_PROXY"] {
        command.env(key, manager.socks_url.as_str().trim_end_matches('/'));
    }
    let bypass = std::env::var("CLASH_NO_PROXY").unwrap_or("127.0.0.1,localhost,::1".into());
    command.env("no_proxy", &bypass).env("NO_PROXY", &bypass);
}
pub fn shell_integration(manager: &Manager, command: &str, args: &[String]) -> Result<()> {
    shell::integration(manager, command, args)
}
fn option(args: &[String], name: &str) -> Result<Option<String>> {
    let mut values = args
        .windows(2)
        .filter(|p| p[0] == name)
        .map(|p| p[1].clone());
    let value = values.next();
    if values.next().is_some() || args.last().is_some_and(|s| s == name) {
        bail!("选项缺少参数或重复，请查看命令帮助。")
    }
    Ok(value)
}
fn validate_options(args: &[String], values: &[&str], flags: &[&str]) -> Result<()> {
    let mut i = 0;
    let mut seen = HashSet::new();
    while i < args.len() {
        if !seen.insert(&args[i]) {
            bail!("命令选项重复。")
        }
        if values.contains(&args[i].as_str()) {
            if i + 1 >= args.len() {
                bail!("命令选项缺少参数。")
            }
            i += 2;
        } else if flags.contains(&args[i].as_str()) {
            i += 1;
        } else {
            bail!("未知命令选项，请查看命令帮助。")
        }
    }
    Ok(())
}
fn executable(path: &Path) -> Result<()> {
    fs::set_permissions(path, fs::Permissions::from_mode(0o755))?;
    Ok(())
}
fn build_info(binary: &Path) -> Result<Value> {
    let output = command_output(
        Command::new(binary).arg("--build-info"),
        Duration::from_secs(5),
    )?;
    if !output.status.success() {
        bail!("候选程序无法运行，请检查目标架构。")
    }
    let info: Value = serde_json::from_slice(&output.stdout).context("候选程序版本信息无效。")?;
    if info["format"] != 1
        || info["implementation"] != "rust"
        || info["arch"] != std::env::consts::ARCH
        || info["version"].as_str().is_none()
    {
        bail!("候选程序格式或架构不兼容。")
    }
    Ok(info)
}
fn marker(root: &Path, version: &str) -> Value {
    json!({"format":1,"implementation":"rust","version":version,"prefix":root,"architecture":std::env::consts::ARCH})
}
fn validate_install_at(path: &Path, prefix: &Path) -> Result<Value> {
    if path.symlink_metadata()?.file_type().is_symlink() {
        bail!("安装目录不能是符号链接。")
    }
    let value: Value = serde_json::from_slice(
        &fs::read(path.join(MARKER))
            .context("此目录不是 Rust 安装，请使用独立目录安装 Rust 版本。")?,
    )
    .context("安装标记损坏，请保留数据并检查安装。")?;
    if value["format"] != 1
        || value["implementation"] != "rust"
        || value["prefix"] != prefix.to_string_lossy().as_ref()
        || value["architecture"] != std::env::consts::ARCH
        || value["version"].as_str().is_none()
    {
        bail!("安装标记不匹配，拒绝修改。")
    }
    Ok(value)
}
fn safe_root(root: &Path) -> Result<()> {
    let forbidden = [
        "/",
        "/root",
        "/home",
        "/tmp",
        "/usr",
        "/usr/local",
        "/etc",
        "/var",
        "/bin",
    ];
    if root.parent().is_none()
        || forbidden.iter().any(|p| root == Path::new(p))
        || std::env::var_os("HOME").is_some_and(|home| root == Path::new(&home))
        || root
            .symlink_metadata()
            .is_ok_and(|m| m.file_type().is_symlink())
    {
        bail!("安装路径不安全，拒绝修改。")
    }
    Ok(())
}
fn copy_tree(source: &Path, target: &Path) -> Result<()> {
    let meta = source.symlink_metadata()?;
    if meta.file_type().is_symlink() {
        bail!("安装或数据目录包含符号链接，拒绝迁移。")
    }
    if meta.is_file() {
        private_dir(target.parent().context("文件路径无效。")?)?;
        fs::copy(source, target)?;
    } else if meta.is_dir() {
        private_dir(target)?;
        for entry in fs::read_dir(source)? {
            let entry = entry?;
            copy_tree(&entry.path(), &target.join(entry.file_name()))?;
        }
    } else {
        bail!("安装目录包含不支持的文件类型。")
    }
    Ok(())
}
pub fn self_install(args: &[String]) -> Result<()> {
    validate_options(
        args,
        &[
            "--prefix",
            "--core",
            "--import-data",
            "--archive",
            "--sha256",
        ],
        &["--no-shell", "--skip-core"],
    )?;
    let archive = option(args, "--archive")?;
    let checksum = option(args, "--sha256")?;
    if archive.is_some() != checksum.is_some() {
        bail!("从发行包安装需要同时提供 --archive 和 --sha256。")
    }
    let home = std::env::var_os("HOME").context("缺少用户家目录。")?;
    let prefix = PathBuf::from(option(args, "--prefix")?.unwrap_or_else(|| {
        PathBuf::from(home)
            .join(".local/share/clash-linux-rust")
            .to_string_lossy()
            .into_owned()
    }));
    let absolute = if prefix.is_absolute() {
        prefix
    } else {
        std::env::current_dir()?.join(prefix)
    };
    safe_root(&absolute)?;
    if absolute
        .components()
        .any(|c| matches!(c, Component::ParentDir))
    {
        bail!("安装路径不能包含上级目录。")
    }
    let name = absolute.file_name().context("安装路径无效。")?;
    private_dir(absolute.parent().context("安装路径无效。")?)?;
    let root = fs::canonicalize(absolute.parent().unwrap())?.join(name);
    safe_root(&root)?;
    let _lock = Lock::operation(&root, true)?;
    if root.exists() && fs::read_dir(&root)?.next().is_some() {
        if !root.join(DATA_MARKER).is_file() {
            bail!("目标目录已有文件；已有 Rust 安装请运行 clash upgrade，迁移请使用新的独立目录。")
        }
        let record: Value = serde_json::from_slice(&fs::read(root.join(DATA_MARKER))?)?;
        if record["prefix"] != root.to_string_lossy().as_ref() || record["implementation"] != "rust"
        {
            bail!("保留数据标记与目录不匹配。")
        }
    }
    let stage = tempfile::Builder::new()
        .prefix(".clash-rust-install-")
        .tempdir_in(root.parent().unwrap())?;
    let source = std::env::current_exe()?;
    let info = build_info(&source)?;
    if let (Some(archive), Some(checksum)) = (archive, checksum) {
        unpack(Path::new(&archive), &checksum, stage.path())?;
        if build_info(&stage.path().join("clash"))? != info {
            bail!("安装器与发行包版本不一致，请使用同一发行版本的文件。")
        }
    } else {
        fs::copy(&source, stage.path().join("clash"))?;
        executable(&stage.path().join("clash"))?;
        for name in [
            "README.md",
            "LICENSE",
            "THIRD-PARTY-NOTICES.txt",
            "licenses",
            "BUILD.json",
        ] {
            let path = source.parent().unwrap().join(name);
            if path.exists() {
                copy_tree(&path, &stage.path().join(name))?;
            }
        }
        if !stage.path().join("BUILD.json").exists() {
            atomic(
                &stage.path().join("BUILD.json"),
                serde_json::to_vec(&json!({
                    "implementation":"rust","version":info["version"],"architecture":info["arch"]
                }))?,
            )?;
        }
    }
    if root.exists() {
        for name in ["conf", "runtime/mvp"] {
            if root.join(name).exists() {
                copy_tree(&root.join(name), &stage.path().join(name))?;
            }
        }
    }
    if let Some(import) = option(args, "--import-data")? {
        let source = fs::canonicalize(import).context("迁移来源不存在。")?;
        let _source_lock = Lock::operation(&source, true)?;
        if stage.path().join("runtime/mvp/state.json").exists() {
            bail!("目标已有订阅，不能重复导入。")
        }
        if !source.join("runtime/mvp/state.json").is_file() {
            bail!("迁移来源没有 MVP 订阅记录。")
        }
        if source.join("runtime/mvp/transaction.json").exists() {
            bail!("来源存在未完成事务，请先在原安装恢复。")
        }
        for name in ["conf", "runtime/mvp"] {
            copy_tree(&source.join(name), &stage.path().join(name))?;
        }
        remove_file(&stage.path().join("runtime/mvp/lock"))?;
    }
    private_dir(&stage.path().join("bin"))?;
    if let Some(core) = option(args, "--core")? {
        fs::copy(core, stage.path().join("bin/mihomo"))?;
        executable(&stage.path().join("bin/mihomo"))?;
    }
    if !args.iter().any(|a| a == "--skip-core") {
        install_core(&Manager::new(stage.path().into())?)?;
    }
    atomic(
        &stage.path().join(MARKER),
        serde_json::to_vec_pretty(&marker(&root, info["version"].as_str().unwrap()))?,
    )?;
    atomic(&stage.path().join("env.sh"), shell::environment(&root))?;
    if root.exists() {
        let old = tempfile::Builder::new()
            .prefix(".clash-data-")
            .tempdir_in(root.parent().unwrap())?;
        let backup = old.path().join("data");
        fs::rename(&root, &backup)?;
        if let Err(error) = fs::rename(stage.path(), &root) {
            fs::rename(&backup, &root)?;
            return Err(error.into());
        }
    } else {
        fs::rename(stage.path(), &root)?;
    }
    println!("已安装 Rust 版本：{}", root.display());
    if !args.iter().any(|a| a == "--no-shell") {
        shell::integration(&Manager::new(root)?, "shell-init", &[])?;
    }
    Ok(())
}
fn bounded_download(url: &str, limit: u64) -> Result<Vec<u8>> {
    let parsed = url::Url::parse(url).map_err(|_| anyhow::anyhow!("下载地址无效。"))?;
    if parsed.scheme() != "https" || !parsed.username().is_empty() || parsed.password().is_some() {
        bail!("程序与核心下载必须使用 HTTPS。")
    }
    let response = crate::http::external_agent(&parsed, Duration::from_secs(180))?
        .get(url)
        .call()
        .map_err(|_| anyhow::anyhow!("下载失败，请检查网络或使用离线文件。"))?;
    if !response.get_url().starts_with("https://") {
        bail!("下载重定向必须使用 HTTPS。")
    }
    let mut body = Vec::new();
    response
        .into_reader()
        .take(limit + 1)
        .read_to_end(&mut body)
        .context("下载未完成。")?;
    if body.is_empty() || body.len() as u64 > limit {
        bail!("下载内容为空或超出大小限制。")
    }
    Ok(body)
}
pub fn install_core(manager: &Manager) -> Result<()> {
    let path = manager.root.join("bin/mihomo");
    if path.exists() {
        let output = command_output(Command::new(&path).arg("-v"), Duration::from_secs(5))?;
        if output.status.success()
            && String::from_utf8_lossy(&output.stdout)
                .split_whitespace()
                .any(|v| v == CORE_VERSION)
        {
            if let Ok(bytes) = fs::read(manager.root.join("bin/mihomo.json")) {
                let record: Value = serde_json::from_slice(&bytes).context("核心校验记录损坏。")?;
                if record["sha256"] != digest(&fs::read(&path)?) {
                    bail!("已有核心校验失败，请保留文件并重新安装。")
                }
            }
            println!("已复用 Mihomo {CORE_VERSION}，无需下载。");
            return Ok(());
        }
        if manager.core.running() {
            bail!("核心正在运行，请停止后再更新核心。")
        }
    }
    let arch = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        _ => bail!("发行版仅支持 Linux x86_64 和 ARM64。"),
    };
    let filename = format!("mihomo-linux-{arch}-{CORE_VERSION}.gz");
    let expected = include_str!("../assets/mihomo-v1.19.29.sha256")
        .lines()
        .filter_map(|line| {
            let mut p = line.split_whitespace();
            Some((p.next()?, p.next()?))
        })
        .find(|(_, name)| *name == filename)
        .map(|(hash, _)| hash)
        .context("缺少固定核心校验值。")?;
    let archive = if let Some(path) = std::env::var_os("MIHOMO_ARCHIVE") {
        fs::read(path).context("无法读取离线核心文件。")?
    } else {
        bounded_download(
            &format!(
                "https://github.com/MetaCubeX/mihomo/releases/download/{CORE_VERSION}/{filename}"
            ),
            MAX_ARCHIVE,
        )?
    };
    if digest(&archive) != expected {
        bail!("核心下载 SHA-256 校验失败，原核心未修改。")
    }
    let mut binary = Vec::new();
    GzDecoder::new(archive.as_slice())
        .take(MAX_ARCHIVE + 1)
        .read_to_end(&mut binary)?;
    if binary.len() as u64 > MAX_ARCHIVE {
        bail!("核心解压内容过大。")
    }
    private_dir(path.parent().unwrap())?;
    let mut candidate = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    candidate.write_all(&binary)?;
    candidate.as_file().sync_all()?;
    let candidate = candidate.into_temp_path();
    executable(&candidate)?;
    let output = command_output(Command::new(&candidate).arg("-v"), Duration::from_secs(5))?;
    if !output.status.success()
        || !String::from_utf8_lossy(&output.stdout)
            .split_whitespace()
            .any(|v| v == CORE_VERSION)
    {
        bail!("核心版本或架构不兼容，原核心未修改。")
    }
    candidate
        .persist(&path)
        .map_err(|_| anyhow::anyhow!("无法保存核心。"))?;
    atomic(
        &manager.root.join("bin/mihomo.json"),
        serde_json::to_vec(&json!({"version":CORE_VERSION,"sha256":digest(&binary)}))?,
    )?;
    println!("已安装并校验 Mihomo {CORE_VERSION}。");
    Ok(())
}
fn unpack(archive: &Path, expected: &str, target: &Path) -> Result<()> {
    if expected.len() != 64 || !expected.chars().all(|c| c.is_ascii_hexdigit()) {
        bail!("必须提供有效 SHA-256。")
    }
    let bytes = fs::read(archive).context("无法读取发行包。")?;
    if bytes.len() as u64 > MAX_ARCHIVE || digest(&bytes) != expected.to_lowercase() {
        bail!("发行包 SHA-256 不匹配或文件过大，原安装未修改。")
    }
    let mut tar = tar::Archive::new(GzDecoder::new(bytes.as_slice()));
    let mut names = HashSet::new();
    let mut size = 0u64;
    for entry in tar.entries()? {
        let mut entry = entry?;
        let path = entry.path()?.into_owned();
        if !entry.header().entry_type().is_file()
            || path.is_absolute()
            || path
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
            || !names.insert(path.clone())
        {
            bail!("发行包包含链接、重复文件或无效路径。")
        }
        let name = path.to_string_lossy();
        if ![
            "clash",
            "BUILD.json",
            "README.md",
            "LICENSE",
            "THIRD-PARTY-NOTICES.txt",
        ]
        .contains(&name.as_ref())
            && !name.starts_with("licenses/")
        {
            bail!("发行包包含非程序文件，拒绝覆盖数据或核心。")
        }
        size = size.checked_add(entry.size()).context("发行包大小无效。")?;
        if size > MAX_ARCHIVE {
            bail!("发行包解压内容过大。")
        }
        let path = target.join(&path);
        private_dir(path.parent().unwrap())?;
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        std::io::copy(&mut entry, &mut file)?;
        file.sync_all()?;
    }
    if !names.contains(Path::new("clash")) || !names.contains(Path::new("BUILD.json")) {
        bail!("发行包缺少程序入口或清单。")
    }
    executable(&target.join("clash"))?;
    let build: Value = serde_json::from_slice(&fs::read(target.join("BUILD.json"))?)
        .context("发行包清单无效。")?;
    let info = build_info(&target.join("clash"))?;
    if build["implementation"] != "rust"
        || build["version"] != info["version"]
        || build["architecture"] != info["arch"]
    {
        bail!("发行包清单与程序不匹配。")
    }
    Ok(())
}
#[derive(Serialize, Deserialize)]
struct UpgradeTransaction {
    format: u32,
    implementation: String,
    root: PathBuf,
    stage: PathBuf,
    previous: PathBuf,
    running: bool,
}
fn restore_upgrade(manager: &Manager) -> Result<()> {
    let path = sibling(&manager.root, ".upgrade.json");
    if !path.exists() {
        return Ok(());
    }
    let record: UpgradeTransaction = serde_json::from_slice(&fs::read(&path)?)
        .context("升级事务记录无效，请保留文件并人工检查。")?;
    if record.format != 1
        || record.implementation != "rust"
        || record.root != manager.root
        || record.previous != sibling(&manager.root, ".rust-previous")
        || record.stage.parent() != manager.root.parent()
        || !record
            .stage
            .file_name()
            .is_some_and(|s| s.to_string_lossy().starts_with(".clash-rust-upgrade-"))
    {
        bail!("升级事务路径不匹配，拒绝自动修改。")
    }
    if record.previous.exists() {
        validate_install_at(&record.previous, &manager.root)?;
        if manager.root.exists() {
            validate_install_at(&manager.root, &manager.root)?;
            if manager.core.running() {
                manager.core.stop()?;
            }
            let failed = record.stage.with_extension("failed");
            if failed.exists() {
                bail!("恢复暂存目录已存在，请人工检查。")
            }
            fs::rename(&manager.root, &failed)?;
            if let Err(error) = fs::rename(&record.previous, &manager.root) {
                fs::rename(failed, &manager.root)?;
                return Err(error.into());
            }
        } else {
            fs::rename(&record.previous, &manager.root)?;
        }
    }
    if record.running && !manager.core.running() {
        manager.perform("ensure", &[])?;
    }
    remove_file(&path)?;
    println!("已恢复升级前的安装状态。");
    Ok(())
}
fn upgrade(manager: &Manager, args: &[String]) -> Result<()> {
    validate_options(args, &["--archive", "--sha256", "--version"], &["--check"])?;
    validate_install_at(&manager.root, &manager.root)?;
    if sibling(&manager.root, ".upgrade.json").exists() {
        bail!("存在未完成升级，请先运行 clash recover。")
    }
    let archive = option(args, "--archive")?;
    let checksum = option(args, "--sha256")?;
    let check = args.iter().any(|a| a == "--check");
    if check && archive.is_some() {
        bail!("--check 不能与离线升级包组合使用。")
    }
    let work = tempfile::Builder::new()
        .prefix(".clash-rust-upgrade-")
        .tempdir_in(manager.root.parent().unwrap())?;
    let (archive, checksum) = if let Some(archive) = archive {
        (
            PathBuf::from(archive),
            checksum.context("离线升级需要 --sha256。")?,
        )
    } else {
        if checksum.is_some() {
            bail!("--sha256 必须与 --archive 一起使用。")
        }
        let url = match std::env::var("CLASH_RUST_RELEASE_URL") {
            Ok(url) => url,
            Err(_) if check => {
                println!(
                    "当前 Rust 版本 {}；尚未配置 Rust 发行源。",
                    env!("CARGO_PKG_VERSION")
                );
                return Ok(());
            }
            Err(_) => bail!("尚未配置 Rust 发行源，请使用 --archive 和 --sha256 离线升级。"),
        };
        let metadata: Value = serde_json::from_slice(&bounded_download(&url, 1024 * 1024)?)
            .context("Rust 发行元数据格式无效。")?;
        if metadata["format"] != 1 || metadata["implementation"] != "rust" {
            bail!("发行源不是 Rust 发行版。")
        }
        let version = metadata["version"].as_str().context("发行源缺少版本。")?;
        if let Some(requested) = option(args, "--version")? {
            if requested.trim_start_matches('v') != version.trim_start_matches('v') {
                bail!("指定版本与发行源不匹配。")
            }
        }
        if check {
            println!(
                "当前版本：{}；发行源版本：{version}",
                env!("CARGO_PKG_VERSION")
            );
            return Ok(());
        }
        if version.trim_start_matches('v') == env!("CARGO_PKG_VERSION") {
            println!("当前已是发行源版本，无需下载。");
            return Ok(());
        }
        let asset = &metadata["assets"][std::env::consts::ARCH];
        let url = asset["url"]
            .as_str()
            .context("发行源缺少当前架构下载地址。")?;
        let sum = asset["sha256"]
            .as_str()
            .context("发行源缺少 SHA-256。")?
            .to_owned();
        let archive = work.path().join("package.tar.gz");
        fs::write(&archive, bounded_download(url, MAX_ARCHIVE)?)?;
        (archive, sum)
    };
    let candidate = work.path().join("candidate");
    private_dir(&candidate)?;
    unpack(&archive, &checksum, &candidate)?;
    let info = build_info(&candidate.join("clash"))?;
    if let Some(requested) = option(args, "--version")? {
        if requested.trim_start_matches('v')
            != info["version"].as_str().unwrap().trim_start_matches('v')
        {
            bail!("升级包版本与指定版本不匹配。")
        }
    }
    for name in ["conf", "runtime", "bin", "logs"] {
        if manager.root.join(name).exists() {
            copy_tree(&manager.root.join(name), &candidate.join(name))?;
        }
    }
    remove_file(&candidate.join("runtime/mihomo.pid"))?;
    atomic(
        &candidate.join(MARKER),
        serde_json::to_vec_pretty(&marker(&manager.root, info["version"].as_str().unwrap()))?,
    )?;
    atomic(&candidate.join("env.sh"), shell::environment(&manager.root))?;
    let previous = sibling(&manager.root, ".rust-previous");
    if previous.exists() {
        bail!("上一版本备份已存在。请验证当前版本，并将上一备份移到其他位置后再升级。")
    }
    let record = UpgradeTransaction {
        format: 1,
        implementation: "rust".into(),
        root: manager.root.clone(),
        stage: work.path().into(),
        previous: previous.clone(),
        running: manager.core.running(),
    };
    atomic(
        &sibling(&manager.root, ".upgrade.json"),
        serde_json::to_vec(&record)?,
    )?;
    let result = (|| -> Result<()> {
        if record.running {
            manager.core.stop()?;
        }
        fs::rename(&manager.root, &previous)?;
        fs::rename(&candidate, &manager.root)?;
        if record.running {
            manager.perform("ensure", &[])?;
        }
        remove_file(&sibling(&manager.root, ".upgrade.json"))?;
        Ok(())
    })();
    if result.is_err() {
        restore_upgrade(manager).context("升级失败且回退未完成，请运行 clash recover。")?;
        return result;
    }
    println!("程序已升级，配置、订阅和 Mihomo 核心已保留。可运行 clash rollback 回滚。");
    Ok(())
}
fn rollback(manager: &Manager, args: &[String]) -> Result<()> {
    if !args.is_empty() {
        bail!("用法：clash rollback")
    }
    validate_install_at(&manager.root, &manager.root)?;
    let previous = sibling(&manager.root, ".rust-previous");
    validate_install_at(&previous, &manager.root).context("没有可回滚的有效 Rust 版本。")?;
    let work = tempfile::tempdir_in(manager.root.parent().unwrap())?;
    let archive = work.path().join("rollback.tar.gz");
    let gzip =
        flate2::write::GzEncoder::new(fs::File::create(&archive)?, flate2::Compression::default());
    let mut tar = tar::Builder::new(gzip);
    for name in [
        "clash",
        "BUILD.json",
        "README.md",
        "LICENSE",
        "THIRD-PARTY-NOTICES.txt",
    ] {
        if previous.join(name).is_file() {
            tar.append_path_with_name(previous.join(name), name)?;
        }
    }
    if previous.join("licenses").is_dir() {
        // Only regular license files are added; directory records are not needed.
        append_files(&mut tar, &previous.join("licenses"), Path::new("licenses"))?;
    }
    tar.into_inner()?.finish()?;
    let saved = work.path().join("saved-previous");
    fs::rename(&previous, &saved)?;
    let checksum = digest(&fs::read(&archive)?);
    let result = upgrade(
        manager,
        &[
            "--archive".into(),
            archive.to_string_lossy().into_owned(),
            "--sha256".into(),
            checksum,
        ],
    );
    if result.is_err() && !previous.exists() {
        fs::rename(saved, previous)?;
    }
    result
}
fn append_files<W: Write>(tar: &mut tar::Builder<W>, path: &Path, name: &Path) -> Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_symlink() {
            bail!("许可证目录包含符号链接。")
        }
        if entry.file_type()?.is_dir() {
            append_files(tar, &entry.path(), &name.join(entry.file_name()))?;
        } else {
            tar.append_path_with_name(entry.path(), name.join(entry.file_name()))?;
        }
    }
    Ok(())
}
fn remove_owned(path: &Path) -> Result<()> {
    let meta = path.symlink_metadata()?;
    if meta.is_dir() && !meta.file_type().is_symlink() {
        fs::remove_dir_all(path)?;
    } else {
        fs::remove_file(path)?;
    }
    Ok(())
}
fn uninstall(manager: &Manager, args: &[String]) -> Result<()> {
    if !args.is_empty() && args != ["--purge"] {
        bail!("用法：clash uninstall [--purge]")
    }
    safe_root(&manager.root)?;
    let metadata = validate_install_at(&manager.root, &manager.root)?;
    let previous = sibling(&manager.root, ".rust-previous");
    if args == ["--purge"] && previous.exists() {
        validate_install_at(&previous, &manager.root)?;
    }
    let allowed = [
        "clash",
        "BUILD.json",
        "README.md",
        "LICENSE",
        "THIRD-PARTY-NOTICES.txt",
        "licenses",
        "env.sh",
        MARKER,
        "conf",
        "runtime",
        "bin",
        "logs",
        DATA_MARKER,
    ];
    for entry in fs::read_dir(&manager.root)? {
        let entry = entry?;
        if !allowed.contains(&entry.file_name().to_string_lossy().as_ref()) {
            bail!("安装目录包含非程序文件，拒绝自动卸载。")
        }
        if entry.file_type()?.is_symlink() {
            bail!("安装目录包含符号链接，拒绝自动卸载。")
        }
    }
    crate::maintenance::remove_schedule(manager)?;
    if manager.core.running() {
        manager.core.stop()?;
    }
    if let Some(home) = std::env::var_os("HOME").map(PathBuf::from) {
        for (rc, name) in [(".bashrc", "bash"), (".zshrc", "zsh")] {
            if fs::read_to_string(home.join(rc)).is_ok_and(|s| {
                s.contains(&manager.root.to_string_lossy().to_string())
                    && s.contains("# >>> clash_linux shell integration >>>")
            }) {
                shell::integration(manager, "shell-uninstall", &["--shell".into(), name.into()])?;
            }
        }
    }
    let purge = args == ["--purge"];
    if !purge {
        atomic(
            &manager.root.join(DATA_MARKER),
            serde_json::to_vec(&metadata)?,
        )?;
    }
    for entry in fs::read_dir(&manager.root)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !purge && (name == "conf" || name == DATA_MARKER) {
            continue;
        }
        if !purge && name == "runtime" {
            for child in fs::read_dir(entry.path())? {
                let child = child?;
                if child.file_name() != "mvp" {
                    remove_owned(&child.path())?;
                }
            }
            continue;
        }
        remove_owned(&entry.path())?;
    }
    if purge {
        if previous.exists() {
            remove_owned(&previous)?;
        }
        fs::remove_dir(&manager.root)?;
    }
    println!(
        "{}",
        if purge {
            "已卸载并删除当前安装数据。"
        } else {
            "已卸载程序，订阅与配置已保留，可在原目录重新安装。"
        }
    );
    Ok(())
}
pub fn manage(manager: &Manager, command: &str, args: &[String]) -> Result<()> {
    match command {
        "upgrade" => upgrade(manager, args),
        "rollback" => rollback(manager, args),
        "recover" => {
            if !args.is_empty() {
                bail!("用法：clash recover")
            }
            restore_upgrade(manager)
        }
        "uninstall" => uninstall(manager, args),
        _ => bail!("未知发行操作。"),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn archive_checksum_failure_preserves_target() {
        let work = tempfile::tempdir().unwrap();
        let archive = work.path().join("bad.tar.gz");
        fs::write(&archive, b"bad").unwrap();
        let target = work.path().join("target");
        fs::create_dir(&target).unwrap();
        assert!(unpack(&archive, &"0".repeat(64), &target).is_err());
        assert_eq!(fs::read_dir(target).unwrap().count(), 0);
    }
    #[test]
    fn rejects_unmanaged_installations_and_critical_roots() {
        let work = tempfile::tempdir().unwrap();
        fs::write(work.path().join("mine"), "keep").unwrap();
        assert!(validate_install_at(work.path(), work.path()).is_err());
        assert_eq!(
            fs::read_to_string(work.path().join("mine")).unwrap(),
            "keep"
        );
        for path in ["/", "/root", "/usr", "/tmp", "/etc"] {
            assert!(safe_root(Path::new(path)).is_err());
        }
    }
    #[test]
    fn rejects_unknown_and_duplicate_options() {
        assert!(validate_options(&["--bad".into()], &[], &[]).is_err());
        assert!(option(&["--prefix".into()], "--prefix").is_err());
        assert!(
            validate_options(&["--check".into(), "--check".into()], &[], &["--check"]).is_err()
        );
    }
}

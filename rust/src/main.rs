mod backup;
mod distribution;
mod http;
mod lifecycle;
mod maintenance;
mod manager;
mod output;
mod plain;
mod shell;
mod storage;
mod ui;

use anyhow::{bail, Context, Result};
use manager::Manager;
use serde_json::{json, Value};
use std::io::{BufRead, Read, Write};
use std::path::PathBuf;
use storage::Lock;

fn root(args: &mut Vec<String>) -> Result<PathBuf> {
    let explicit = if args.first().is_some_and(|s| s == "--root") {
        if args.len() < 2 {
            bail!("用法：clash --root DIRECTORY [命令]")
        }
        let path = args.remove(1);
        args.remove(0);
        Some(PathBuf::from(path))
    } else {
        std::env::var_os("CLASH_ROOT").map(PathBuf::from)
    };
    let path = explicit.unwrap_or_else(|| {
        let executable = std::env::current_exe().unwrap();
        let parent = executable.parent().unwrap();
        if matches!(
            parent.file_name().and_then(|s| s.to_str()),
            Some("debug" | "release")
        ) && parent
            .parent()
            .and_then(|p| p.file_name())
            .is_some_and(|s| s == "target")
        {
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .to_owned()
        } else {
            parent.to_owned()
        }
    });
    if !path.exists() && args.first().is_some_and(|s| s == "recover") {
        let absolute = if path.is_absolute() {
            path
        } else {
            std::env::current_dir()?.join(path)
        };
        return Ok(
            std::fs::canonicalize(absolute.parent().context("恢复路径无效。")?)?
                .join(absolute.file_name().context("恢复路径无效。")?),
        );
    }
    std::fs::canonicalize(path).context("工作目录不存在，请检查安装路径。")
}

fn worker(manager: &Manager) -> Result<i32> {
    manager.cancel.install()?;
    let result = (|| -> Result<Value> {
        let mut input = std::io::stdin().lock();
        let mut raw = Vec::new();
        (&mut input).take(65537).read_until(b'\n', &mut raw)?;
        if raw.len() > 65536 {
            bail!("操作请求过长。")
        }
        let request: Value = serde_json::from_slice(&raw).context("无效的操作请求。")?;
        let action = request["action"].as_str().context("无效的操作请求。")?;
        let args = match request.get("args") {
            Some(Value::Array(args)) => args.as_slice(),
            None => &[],
            _ => bail!("无效的操作请求。"),
        };
        manager.perform(action, args)
    })();
    let payload = output::envelope(result);
    let code = if payload["ok"] == true { 0 } else { 1 };
    println!("{payload}");
    std::io::stdout().flush()?;
    Ok(code)
}

fn no_args(args: &[String]) -> Result<()> {
    if !args.is_empty() {
        bail!("此命令不接受额外参数，请运行 clash help。")
    };
    Ok(())
}

fn run() -> Result<i32> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().is_some_and(|s| s == "--build-info") {
        println!(
            "{}",
            json!({"format":1,"implementation":"rust","version":env!("CARGO_PKG_VERSION"),"arch":std::env::consts::ARCH})
        );
        return Ok(0);
    }
    if args.first().is_some_and(|s| s == "self-install") {
        distribution::self_install(&args[1..])?;
        return Ok(0);
    }
    let root = root(&mut args)?;
    let command = if args.is_empty() {
        "menu".to_owned()
    } else {
        args.remove(0)
    };
    if matches!(command.as_str(), "help" | "--help" | "-h") {
        print!("{}", include_str!("../HELP.txt"));
        return Ok(0);
    }
    let manager = Manager::new(root)?;
    match command.as_str() {
        "env" => {
            no_args(&args)?;
            distribution::print_env(&manager);
            return Ok(0);
        }
        "off" => {
            no_args(&args)?;
            println!("unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY ftp_proxy FTP_PROXY socks_proxy SOCKS_PROXY rsync_proxy RSYNC_PROXY");
            return Ok(0);
        }
        "app-version" => {
            no_args(&args)?;
            println!("Clash Linux {} (Rust)", env!("CARGO_PKG_VERSION"));
            return Ok(0);
        }
        "shell-init" | "shell-uninstall" => {
            let _lock = Lock::operation(&manager.root, false)?;
            distribution::shell_integration(&manager, &command, &args)?;
            return Ok(0);
        }
        "upgrade" | "rollback" | "recover" | "uninstall" => {
            let _lock = Lock::operation(&manager.root, true)?;
            distribution::manage(&manager, &command, &args)?;
            return Ok(0);
        }
        _ => {}
    }
    let _lock = Lock::operation(&manager.root, false)?;
    match command.as_str() {
        "subscription" => {
            manager.cancel.install()?;
            match args.first().map(String::as_str) {
                Some("status" | "auto-update") if args.len() == 1 => println!(
                    "{}",
                    serde_json::to_string_pretty(&maintenance::status(&manager)?)?
                ),
                Some("auto-update") if args.len() == 2 => println!(
                    "{}",
                    manager
                        .perform("auto_update_set", &[json!(args[1])])?
                        .as_str()
                        .unwrap_or("")
                ),
                Some("update" | "update-due") if args.len() == 1 => println!(
                    "{}",
                    manager
                        .perform(
                            if args[0] == "update" {
                                "update_all"
                            } else {
                                "update_due"
                            },
                            &[]
                        )?
                        .as_str()
                        .unwrap_or("")
                ),
                _ => {
                    bail!("用法：clash subscription status|update|update-due|auto-update [6h|off]")
                }
            }
        }
        "backup" => {
            manager.cancel.install()?;
            let (action, values) = match args.first().map(String::as_str) {
                Some("create") if args.len() == 1 => ("backup_create", vec![]),
                Some("list") if args.len() == 1 => ("backup_list", vec![]),
                Some("restore") if args.len() == 2 => ("backup_restore", vec![json!(args[1])]),
                Some("auto") if args.len() == 2 => ("backup_auto_set", vec![json!(args[1])]),
                Some("keep") if args.len() == 2 => ("backup_keep", vec![json!(args[1])]),
                _ => bail!("用法：clash backup create|list|restore NAME|auto on|off|keep COUNT"),
            };
            let result = manager.perform(action, &values)?;
            if let Some(message) = result.as_str() {
                println!("{message}");
            } else {
                println!("{}", serde_json::to_string_pretty(&result)?);
            }
        }
        "worker" => {
            no_args(&args)?;
            return worker(&manager);
        }
        "menu" => {
            if args.is_empty()
                && std::env::var("TERM").is_ok_and(|term| !term.is_empty() && term != "dumb")
            {
                ui::run(&manager)?;
            } else if args.is_empty() || args == ["--plain"] {
                plain::run(&manager)?;
            } else {
                bail!("用法：clash menu [--plain]")
            }
        }
        "status" => {
            no_args(&args)?;
            let status = manager.status()?;
            println!(
                "核心：{}\n当前终端代理：{}\n订阅：{}\n模式：{} / 节点：{}",
                match output::text(&status, "core_state") {
                    "running" => "运行中",
                    "unhealthy" => "进程运行中，接口异常",
                    _ => "已停止",
                },
                output::text(&status, "shell_proxy"),
                output::clean(output::text(&status, "name")),
                output::text(&status, "mode"),
                output::clean(output::text(&status, "node"))
            );
        }
        "start" | "ensure" | "on" | "stop" | "restart" => {
            no_args(&args)?;
            manager.cancel.install()?;
            let action = if matches!(command.as_str(), "start" | "on") {
                "ensure"
            } else {
                &command
            };
            let message = manager.perform(action, &[])?;
            if matches!(command.as_str(), "on" | "ensure") {
                eprintln!("{}", message.as_str().unwrap_or(""));
            } else {
                println!("{}", message.as_str().unwrap_or(""));
            }
            if command == "on" {
                distribution::print_env(&manager);
            }
        }
        "install" | "update" => {
            no_args(&args)?;
            distribution::install_core(&manager)?;
        }
        "version" => {
            no_args(&args)?;
            let output = lifecycle::command_output(
                std::process::Command::new(manager.core.require_binary()?).arg("-v"),
                std::time::Duration::from_secs(5),
            )?;
            if !output.status.success() {
                bail!("无法读取核心版本。")
            };
            print!("{}", String::from_utf8_lossy(&output.stdout));
        }
        "doctor" => {
            no_args(&args)?;
            for line in manager
                .perform("diagnose", &[])?
                .as_array()
                .context("诊断结果异常。")?
            {
                println!("{}", output::clean(line.as_str().unwrap_or("")));
            }
        }
        "shell" => {
            no_args(&args)?;
            manager.perform("ensure", &[])?;
            use std::os::unix::process::CommandExt;
            let shell = std::env::var("CLASH_SHELL")
                .or_else(|_| std::env::var("SHELL"))
                .unwrap_or("/bin/bash".into());
            let mut child = std::process::Command::new(shell);
            distribution::proxy_environment(&manager, &mut child);
            drop(_lock);
            return Err(child.exec().into());
        }
        "logs" => {
            if !args.is_empty() && args != ["--follow"] && args != ["-f"] {
                bail!("用法：clash logs [--follow]")
            };
            let mut child = std::process::Command::new("tail");
            child.args(["-n", "100"]);
            if !args.is_empty() {
                child.arg("-f");
            }
            let status = child
                .arg("--")
                .arg(manager.root.join("logs/mihomo.log"))
                .status()?;
            return Ok(status.code().unwrap_or(1));
        }
        "init" | "mode" | "switch" => {
            bail!("已启用多订阅管理。请运行 clash 菜单修改配置、模式或节点。")
        }
        _ => bail!("未知命令，请运行 clash help 查看帮助。"),
    }
    Ok(0)
}
fn main() {
    std::process::exit(match run() {
        Ok(code) => code,
        Err(error) => {
            eprintln!("错误：{}", output::safe_error(&error));
            1
        }
    });
}

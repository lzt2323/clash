use crate::manager::Manager;
use crate::storage::atomic;
use anyhow::{bail, Context, Result};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const BEGIN: &str = "# >>> clash_linux shell integration >>>";
const END: &str = "# <<< clash_linux shell integration <<<";
pub fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}
pub fn environment(root: &Path) -> String {
    include_str!("../shell-integration.sh")
        .replace("@CLI@", &quote(&root.join("clash").to_string_lossy()))
}
fn rc_path(args: &[String]) -> Result<PathBuf> {
    let shell = if args.is_empty() {
        std::env::var("SHELL").unwrap_or_else(|_| "bash".into())
    } else if args.len() == 2 && args[0] == "--shell" {
        args[1].clone()
    } else {
        bail!("用法：clash shell-init [--shell bash|zsh]")
    };
    let home = PathBuf::from(std::env::var_os("HOME").context("缺少用户家目录。")?);
    Ok(home.join(
        match Path::new(&shell).file_name().and_then(|s| s.to_str()) {
            Some("bash") => ".bashrc",
            Some("zsh") => ".zshrc",
            _ => bail!("仅支持 Bash 和 Zsh。请指定 --shell bash 或 zsh。"),
        },
    ))
}
fn strip_block(contents: &str, root: &Path) -> Result<String> {
    let mut output = String::new();
    let mut block = String::new();
    let mut inside = false;
    for line in contents.lines() {
        if line == BEGIN {
            if inside {
                bail!("shell 配置集成标记不完整，请先修复配置。")
            };
            inside = true;
            block.clear();
        }
        if inside {
            block.push_str(line);
            block.push('\n');
        } else {
            output.push_str(line);
            output.push('\n');
        }
        if line == END {
            if !inside {
                bail!("shell 配置集成标记不完整。")
            };
            inside = false;
            if !block.contains(&root.to_string_lossy().to_string()) {
                bail!("shell 集成属于其他安装，请先移除原安装的集成。")
            }
        }
    }
    if inside {
        bail!("shell 配置集成标记不完整。")
    };
    Ok(output)
}
pub fn integration(manager: &Manager, command: &str, args: &[String]) -> Result<()> {
    let rc = rc_path(args)?;
    let home = rc.parent().unwrap();
    let shim = home.join(".local/bin/clash");
    let expected = format!(
        "#!/bin/sh\nexec {} \"$@\"\n",
        quote(&manager.root.join("clash").to_string_lossy())
    );
    let original = match fs::read_to_string(&rc) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(_) => bail!("无法读取 shell 配置。"),
    };
    let mut contents = strip_block(&original, &manager.root)?;
    if command == "shell-init" {
        if fs::symlink_metadata(&shim).is_ok()
            && fs::read_to_string(&shim).ok().as_deref() != Some(&expected)
        {
            bail!("已有 clash 命令属于其他安装，请保留它并先解决命令冲突。")
        }
        atomic(&manager.root.join("env.sh"), environment(&manager.root))?;
        let env = quote(&manager.root.join("env.sh").to_string_lossy());
        contents.push_str(&format!(
            "\n{BEGIN}\nif [ -r {env} ]; then . {env}; fi\n{END}\n"
        ));
        atomic(&shim, expected)?;
        fs::set_permissions(&shim, fs::Permissions::from_mode(0o755))?;
        atomic(&rc, contents)?;
        println!(
            "已安装命令与 shell 集成。当前窗口执行：\nsource {}",
            quote(&manager.root.join("env.sh").to_string_lossy())
        );
    } else {
        if original != contents {
            atomic(&rc, contents)?;
        }
        if fs::read_to_string(&shim).ok().as_deref() == Some(&expected) {
            fs::remove_file(shim)?;
        }
        println!("已移除当前安装的 shell 集成。");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn quoting_does_not_execute_shell_metacharacters() {
        assert_eq!(quote("a'b$(touch x)"), "'a'\\''b$(touch x)'");
    }
    #[test]
    fn refuses_to_replace_another_installation() {
        let text = format!("hello\n{BEGIN}\n. /other/env.sh\n{END}\n");
        assert!(strip_block(&text, Path::new("/ours")).is_err());
    }
    #[test]
    fn removes_only_owned_marked_block() {
        let text = format!("hello\n{BEGIN}\n. /ours/env.sh\n{END}\nworld\n");
        assert_eq!(
            strip_block(&text, Path::new("/ours")).unwrap(),
            "hello\nworld\n"
        );
    }
}

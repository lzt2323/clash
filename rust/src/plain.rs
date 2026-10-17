use crate::manager::Manager;
use crate::output::{active, clean, delay_label, mode_label, node_label, safe_error, text, MODES};
use crate::storage::Cancelled;
use anyhow::{bail, Result};
use crossterm::event::{self, Event, KeyCode, KeyModifiers};
use serde_json::{json, Value};
use std::io::{self, IsTerminal, Write};
use std::sync::atomic::Ordering;

#[derive(Debug)]
struct EndOfInput;
impl std::fmt::Display for EndOfInput {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "输入已结束。")
    }
}
impl std::error::Error for EndOfInput {}

fn say(message: &str) {
    for line in message.lines() {
        println!("{}", clean(line));
    }
}
fn read(prompt: &str) -> Result<String> {
    say(prompt);
    print!("> ");
    io::stdout().flush()?;
    let mut line = String::new();
    if io::stdin().read_line(&mut line)? == 0 {
        return Err(EndOfInput.into());
    }
    Ok(line.trim().into())
}
fn secret() -> Result<String> {
    say("请输入订阅地址（输入内容隐藏）");
    print!("> ");
    io::stdout().flush()?;
    crossterm::terminal::enable_raw_mode()?;
    let result = (|| -> Result<String> {
        let mut value = String::new();
        loop {
            if let Event::Key(key) = event::read()? {
                match key.code {
                    KeyCode::Enter => return Ok(value.trim().into()),
                    KeyCode::Char('j' | 'm') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        return Ok(value.trim().into());
                    }
                    KeyCode::Esc => return Err(Cancelled.into()),
                    KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        return Err(Cancelled.into())
                    }
                    KeyCode::Char('d') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        return Err(EndOfInput.into())
                    }
                    KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        value.clear()
                    }
                    KeyCode::Backspace => {
                        value.pop();
                    }
                    KeyCode::Char(c)
                        if !c.is_control()
                            && !key.modifiers.contains(KeyModifiers::CONTROL)
                            && value.chars().count() < 4096 =>
                    {
                        value.push(c)
                    }
                    _ => {}
                }
            }
        }
    })();
    crossterm::terminal::disable_raw_mode()?;
    println!();
    result
}
fn execute(manager: &Manager, action: &str, args: Vec<Value>) -> Result<Value> {
    manager.perform(action, &args)
}
fn show(manager: &Manager, action: &str, args: Vec<Value>) -> Result<()> {
    let result = execute(manager, action, args)?;
    say(result.as_str().unwrap_or("操作已完成。"));
    Ok(())
}
fn header(state: &Value) {
    say("\nClash 终端菜单");
    say(&format!(
        "核心：{}",
        if state["running"] == true {
            "就绪"
        } else {
            "未运行或接口异常"
        }
    ));
    say(&format!("当前终端代理：{}", text(state, "shell_proxy")));
    say(&format!("当前订阅：{}", text(state, "name")));
    say(&format!("模式：{}", mode_label(text(state, "mode"))));
    say(&format!(
        "节点：{}",
        if state["active"].is_null() {
            "未选择".into()
        } else {
            node_label(text(state, "node"))
        }
    ));
}
fn nodes(manager: &Manager) -> Result<()> {
    let (mut query, mut page, mut measured) = (String::new(), 0usize, json!({}));
    loop {
        let state = manager.status()?;
        let Some(subscription) = active(&state) else {
            say("请先在订阅管理中添加并使用订阅。");
            return Ok(());
        };
        let mut nodes = vec!["AUTO".to_owned()];
        nodes.extend(
            subscription["nodes"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .filter(|n| !["AUTO", "PROXY"].contains(n))
                .map(str::to_owned),
        );
        let visible: Vec<_> = nodes
            .iter()
            .filter(|n| node_label(n).to_lowercase().contains(&query.to_lowercase()))
            .collect();
        let pages = visible.len().div_ceil(8).max(1);
        page = page.min(pages - 1);
        let entries: Vec<_> = visible.iter().skip(page * 8).take(8).copied().collect();
        say(&format!("\n选择节点  第 {}/{} 页", page + 1, pages));
        if !query.is_empty() {
            say(&format!("搜索：{query}"));
        }
        for (i, node) in entries.iter().enumerate() {
            say(&format!(
                "{} {}{}",
                i + 1,
                if state["node"] == **node {
                    "[当前] "
                } else {
                    ""
                },
                node_label(node)
            ));
            if let Some(delay) = measured.get(*node) {
                say(&format!("  延迟：{}", delay_label(Some(delay))));
            }
        }
        if entries.is_empty() {
            say("没有匹配的节点。");
        }
        say("n 下一页 / p 上一页\ns 搜索 / t 全部测速 / 0 返回");
        let choice = read("请输入编号")?;
        match choice.as_str() {
            "0" => return Ok(()),
            "n" => page = (page + 1).min(pages - 1),
            "p" => page = page.saturating_sub(1),
            "s" => {
                query = read("输入搜索词，留空显示全部")?;
                page = 0;
            }
            "t" => {
                say("正在测速全部节点，Ctrl+C 取消。");
                measured = execute(manager, "delays_all", vec![])?;
            }
            _ => {
                if let Ok(index) = choice.parse::<usize>() {
                    if let Some(node) = index.checked_sub(1).and_then(|i| entries.get(i)) {
                        show(manager, "select", vec![json!(node)])?;
                        return Ok(());
                    }
                    say("无效选择，请输入本页编号或菜单命令。");
                } else {
                    say("无效选择，请输入本页编号或菜单命令。");
                }
            }
        }
    }
}
fn modes(manager: &Manager) -> Result<()> {
    loop {
        say("选择模式\n1 规则（按当前配置分流）\n2 全局（全部交给所选代理）\n3 直连（已接入的流量直接访问）\n模式只影响接入内核的流量。\n默认规则：私网直连，其他走代理。\n0 返回");
        match read("请输入编号")?.as_str() {
            "0" => return Ok(()),
            c @ ("1" | "2" | "3") => {
                return show(
                    manager,
                    "set_mode",
                    vec![json!(MODES[c.parse::<usize>()? - 1].0)],
                )
            }
            _ => say("无效选择，请输入 0 到 3。"),
        }
    }
}
fn add(manager: &Manager) -> Result<()> {
    let name = read("为订阅起一个名称（0 返回）")?;
    if name == "0" {
        return Ok(());
    }
    if name.is_empty() {
        say("订阅名称不能为空。");
        return Ok(());
    }
    let url = secret()?;
    if url.is_empty() {
        say("订阅地址为空，已取消添加。");
        return Ok(());
    }
    say("正在读取订阅，Ctrl+C 取消。");
    show(manager, "add", vec![json!(name), json!(url)])
}
fn detail(manager: &Manager, id: &str) -> Result<()> {
    loop {
        let state = manager.status()?;
        let Some(item) = state["subscriptions"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|s| s["id"] == id)
        else {
            return Ok(());
        };
        let is_active = state["active"] == id;
        say(&format!(
            "\n订阅：{}\n状态：{}\n节点数：{}\n更新时间：{}",
            text(item, "name"),
            if is_active {
                "正在使用"
            } else {
                "未使用"
            },
            item["nodes"].as_array().map_or(0, Vec::len),
            text(item, "updated")
        ));
        say("1 使用 / 2 更新\n3 编辑地址 / 4 删除 / 0 返回");
        match read("请输入编号")?.as_str() {
            "0" => return Ok(()),
            "1" => show(manager, "use", vec![json!(id)])?,
            "2" => {
                say("正在更新订阅，Ctrl+C 取消。");
                show(manager, "update", vec![json!(id)])?;
            }
            "3" => {
                let url = secret()?;
                if url.is_empty() {
                    say("地址为空，已取消编辑。");
                } else {
                    show(manager, "edit", vec![json!(id), json!(url)])?;
                }
            }
            "4" => {
                if is_active {
                    say(
                        if state["subscriptions"]
                            .as_array()
                            .is_some_and(|s| s.len() == 1)
                        {
                            "删除最后一个订阅会停止核心。删除后请运行 clash off 清理当前终端代理。"
                        } else {
                            "删除当前订阅会自动切换到其他订阅。"
                        },
                    );
                }
                if read("确认删除此订阅？输入 y 确认，其余取消")?.eq_ignore_ascii_case("y")
                {
                    show(manager, "remove", vec![json!(id)])?;
                    return Ok(());
                }
                say("已取消删除。");
            }
            _ => say("无效选择，请输入 0 到 4。"),
        }
    }
}
fn subscriptions(manager: &Manager) -> Result<()> {
    let mut page = 0usize;
    loop {
        let state = manager.status()?;
        let items = state["subscriptions"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let pages = items.len().div_ceil(8).max(1);
        page = page.min(pages - 1);
        let entries: Vec<_> = items.iter().skip(page * 8).take(8).collect();
        say(&format!("\n订阅管理  第 {}/{} 页", page + 1, pages));
        for (i, item) in entries.iter().enumerate() {
            say(&format!(
                "{} {}{}",
                i + 1,
                text(item, "name"),
                if item["id"] == state["active"] {
                    " [当前]"
                } else {
                    ""
                }
            ));
        }
        if items.is_empty() {
            say("尚未添加订阅。首个订阅会自动使用。");
        }
        say("a 添加 / n 下一页 / p 上一页\n输入编号查看详情 / 0 返回");
        let choice = read("请输入编号")?;
        match choice.as_str() {
            "0" => return Ok(()),
            "a" => add(manager)?,
            "n" => page = (page + 1).min(pages - 1),
            "p" => page = page.saturating_sub(1),
            _ => {
                if let Ok(index) = choice.parse::<usize>() {
                    if let Some(item) = index.checked_sub(1).and_then(|i| entries.get(i)) {
                        detail(manager, text(item, "id"))?;
                        continue;
                    }
                }
                say("无效选择，请输入本页编号或菜单命令。");
            }
        }
    }
}
fn diagnose(manager: &Manager) -> Result<()> {
    let check = || -> Result<()> {
        say("正在检查连接，Ctrl+C 取消。");
        if let Some(lines) = execute(manager, "diagnose", vec![])?.as_array() {
            for line in lines {
                say(line.as_str().unwrap_or(""));
            }
        }
        Ok(())
    };
    check()?;
    loop {
        say("\n连接诊断与恢复\n1 重新检测 / 2 重启核心\n3 停止核心 / 0 返回");
        match read("请输入编号")?.as_str() {
            "0" => return Ok(()),
            "1" => check()?,
            c @ ("2" | "3") => {
                let action = if c == "2" { "restart" } else { "stop" };
                say(if c == "2" {
                    "重启核心会中断正在使用它的连接，也会影响其他终端。"
                } else {
                    "停止核心会断开其他使用它的终端。\n停止后请退出菜单并运行 clash off，清理当前终端代理。"
                });
                if read("确认操作？输入 y 确认，其余取消")?.eq_ignore_ascii_case("y")
                {
                    show(manager, action, vec![])?;
                } else {
                    say("已取消操作。");
                }
            }
            _ => say("无效选择，请输入 0 到 3。"),
        }
    }
}
fn maintenance(manager: &Manager) -> Result<()> {
    loop {
        let status = crate::maintenance::status(manager)?;
        let settings = &status["settings"];
        let seconds = settings["update_interval_secs"].as_u64().unwrap_or(0);
        say(&format!(
            "\n设置与备份\n自动更新：{}\n更新前备份：{}；保留 {} 份",
            if seconds == 0 {
                "关闭".into()
            } else {
                format!("每 {} 分钟", seconds / 60)
            },
            if settings["backup_before_update"] == true {
                "开启"
            } else {
                "关闭"
            },
            settings["backup_keep"]
        ));
        if let Some(message) = status["last_run"]["message"].as_str() {
            say(message);
        }
        say("1 设置自动更新间隔 / 2 立即更新全部订阅\n3 立即备份 / 4 恢复备份\n5 切换更新前自动备份 / 6 保留份数 / 0 返回");
        match read("请输入编号")?.as_str() {
            "0" => return Ok(()),
            "1" => show(
                manager,
                "auto_update_set",
                vec![json!(read("输入 30m / 6h / 1d，off 关闭")?)],
            )?,
            "2" => show(manager, "update_all", vec![])?,
            "3" => show(manager, "backup_create", vec![])?,
            "4" => {
                let backups = crate::backup::list(manager)?;
                for (i, name) in backups.iter().enumerate() {
                    say(&format!("{} {name}", i + 1));
                }
                if backups.is_empty() {
                    say("暂无备份。");
                    continue;
                }
                let input = read("输入备份编号，0 返回")?;
                if let Some(name) = input
                    .parse::<usize>()
                    .ok()
                    .and_then(|n| n.checked_sub(1))
                    .and_then(|n| backups.get(n))
                {
                    if read("恢复将替换订阅、节点和模式，并备份当前配置。输入 y 确认")?
                        .eq_ignore_ascii_case("y")
                    {
                        show(manager, "backup_restore", vec![json!(name)])?;
                    }
                }
            }
            "5" => show(
                manager,
                "backup_auto_set",
                vec![json!(if settings["backup_before_update"] == true {
                    "off"
                } else {
                    "on"
                })],
            )?,
            "6" => show(
                manager,
                "backup_keep",
                vec![json!(read("输入保留份数：1–100")?)],
            )?,
            _ => say("无效选择，请输入 0 到 6。"),
        }
    }
}
pub fn run(manager: &Manager) -> Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        bail!("菜单需要交互终端。请在 SSH 终端运行 clash，或使用 clash help 查看命令。")
    }
    manager.cancel.install()?;
    loop {
        let result = (|| -> Result<bool> {
            match manager.status() {
                Ok(state) => header(&state),
                Err(error) => say(&format!("状态暂不可读：{}", safe_error(&error))),
            }
            say("1 节点 / 2 模式\n3 订阅 / 4 诊断 / 0 退出\n5 设置与备份");
            match read("请输入编号")?.as_str() {
                "0" => {
                    say("已退出菜单。");
                    return Ok(false);
                }
                "1" => nodes(manager)?,
                "2" => modes(manager)?,
                "3" => subscriptions(manager)?,
                "4" => diagnose(manager)?,
                "5" => maintenance(manager)?,
                _ => say("无效选择，请输入 0 到 5。"),
            };
            Ok(true)
        })();
        match result {
            Ok(false) => return Ok(()),
            Ok(true) => {}
            Err(error) if error.downcast_ref::<EndOfInput>().is_some() => {
                say("输入已结束，已退出菜单。");
                return Ok(());
            }
            Err(error) => {
                say(&format!("操作未完成：{}", safe_error(&error)));
                manager.cancel.0.store(false, Ordering::Relaxed);
            }
        }
    }
}

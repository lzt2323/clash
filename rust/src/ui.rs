use crate::manager::Manager;
use crate::output::{active, clean, clip, delay_label, mode_label, node_label, text, MODES};
use crate::storage::Cancellation;
use anyhow::{bail, Context, Result};
use crossterm::event::{self, Event, KeyCode as K, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;
use serde_json::{json, Value};
use std::fs::File;
use std::io::{IsTerminal, Read, Seek, SeekFrom, Write};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

const PAGES: [&str; 5] = ["节点", "模式", "订阅", "诊断", "设置与备份"];
pub struct Worker {
    pub action: String,
    process: Child,
    output: File,
    pub started: Instant,
    pub cancelled: bool,
    finished: bool,
}
impl Worker {
    fn start(manager: &Manager, action: &str, args: Vec<Value>) -> Result<Self> {
        let output = tempfile::tempfile()?;
        let executable = std::env::var_os("CLASH_WORKBENCH_WORKER")
            .map(std::path::PathBuf::from)
            .unwrap_or(std::env::current_exe()?);
        let mut process = Command::new(executable)
            .arg("--root")
            .arg(&manager.root)
            .arg("worker")
            .current_dir(&manager.root)
            .stdin(Stdio::piped())
            .stdout(output.try_clone()?)
            .stderr(Stdio::null())
            .spawn()
            .context("无法启动后台操作，请检查安装。")?;
        let payload = serde_json::to_vec(&json!({"action":action,"args":args}))?;
        if payload.len() > 65535 {
            let _ = process.kill();
            let _ = process.wait();
            bail!("操作请求过长。")
        }
        let result = process
            .stdin
            .take()
            .context("后台输入不可用。")?
            .write_all(&[payload, b"\n".to_vec()].concat());
        if result.is_err() {
            let _ = process.kill();
            let _ = process.wait();
            bail!("后台操作未正常启动。")
        }
        Ok(Self {
            action: action.into(),
            process,
            output,
            started: Instant::now(),
            cancelled: false,
            finished: false,
        })
    }
    fn cancel(&mut self) {
        if !self.finished && !self.cancelled {
            self.cancelled = true;
            unsafe {
                libc::kill(self.process.id() as i32, libc::SIGINT);
            }
        }
    }
    fn poll(&mut self) -> Result<Option<Value>> {
        let Some(status) = self.process.try_wait()? else {
            return Ok(None);
        };
        self.finished = true;
        self.output.seek(SeekFrom::Start(0))?;
        let mut body = Vec::new();
        (&mut self.output)
            .take((crate::manager::LIMIT + 1) as u64)
            .read_to_end(&mut body)?;
        let payload = serde_json::from_slice::<Value>(&body).ok();
        Ok(Some(match payload {
            Some(payload)
                if payload["ok"].is_boolean() && (payload["ok"] != true || status.success()) =>
            {
                payload
            }
            _ => json!({"ok":false,"error":"后台操作未正常完成，请运行诊断检查状态。"}),
        }))
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        if !self.finished {
            self.cancel();
            // A transaction in its commit phase must finish or roll back before exit.
            let _ = self.process.wait();
        }
    }
}

#[derive(Clone, Debug)]
enum Dialog {
    Confirm {
        title: String,
        body: String,
        action: String,
        args: Vec<Value>,
        yes: bool,
    },
    Form {
        kind: String,
        field: String,
        value: String,
        name: String,
        id: String,
        error: String,
    },
    Help,
}

pub struct Workbench {
    pub state: Value,
    pub page: usize,
    pub nav: bool,
    indices: [usize; 5],
    query: String,
    delays: serde_json::Map<String, Value>,
    diagnostics: Vec<String>,
    message: String,
    error: bool,
    dialog: Option<Dialog>,
    job: Option<Worker>,
    job_label: String,
    refresh: bool,
    diagnose: bool,
    quitting: bool,
    running: bool,
    rows: usize,
}
impl Default for Workbench {
    fn default() -> Self {
        Self {
            state: json!({}),
            page: 0,
            nav: false,
            indices: [0; 5],
            query: String::new(),
            delays: Default::default(),
            diagnostics: vec!["进入诊断后可检查连接并恢复核心。".into()],
            message: "正在读取状态...".into(),
            error: false,
            dialog: None,
            job: None,
            job_label: String::new(),
            refresh: false,
            diagnose: false,
            quitting: false,
            running: true,
            rows: 8,
        }
    }
}
impl Workbench {
    fn notice(&mut self, message: impl Into<String>, error: bool) {
        self.message = message.into();
        self.error = error;
    }
    fn submit(&mut self, manager: &Manager, action: &str, args: Vec<Value>, label: &str) -> bool {
        if self.job.is_some() {
            self.notice("当前操作仍在进行，可继续浏览，请勿重复提交。", false);
            return false;
        }
        match Worker::start(manager, action, args) {
            Ok(job) => {
                self.job = Some(job);
                self.job_label = label.into();
                true
            }
            Err(_) => {
                self.notice("无法启动后台操作，请检查安装。", true);
                false
            }
        }
    }
    fn poll(&mut self, manager: &Manager) -> Result<()> {
        if let Some(job) = self.job.as_mut() {
            if let Some(payload) = job.poll()? {
                let action = job.action.clone();
                self.job = None;
                if payload["ok"] != true {
                    self.notice(
                        if payload["cancelled"] == true {
                            "已取消当前操作。"
                        } else {
                            payload["error"].as_str().unwrap_or("操作失败，请重试。")
                        },
                        payload["cancelled"] != true,
                    );
                    if payload["cancelled"] == true {
                        self.refresh = false;
                        self.diagnose = false;
                    }
                    if action == "diagnose" {
                        self.diagnostics = vec![self.message.clone()];
                    }
                } else {
                    let result = &payload["result"];
                    match action.as_str() {
                        "status" => {
                            if !result.is_object() {
                                self.notice("状态返回格式异常，请重试。", true);
                            } else {
                                if self.state["active"] != result["active"] {
                                    self.delays.clear();
                                    self.indices[0] = 0;
                                }
                                self.state = result.clone();
                                if self.message == "正在读取状态..." {
                                    self.notice("状态已读取。选择项目后按 Enter 执行。", false);
                                }
                            }
                        }
                        "delays" | "delays_all" => {
                            if let Some(map) = result.as_object() {
                                self.delays.extend(map.clone());
                            }
                            self.notice("全部节点测速完成。", false);
                        }
                        "diagnose" => {
                            self.diagnostics = result
                                .as_array()
                                .map(|a| {
                                    a.iter()
                                        .filter_map(Value::as_str)
                                        .map(str::to_owned)
                                        .collect()
                                })
                                .unwrap_or_default();
                            self.notice("检测完成。", false);
                        }
                        _ => {
                            if ["use", "update", "edit"].contains(&action.as_str()) {
                                self.delays.clear();
                            }
                            self.notice(result.as_str().unwrap_or("操作已完成。"), false);
                            self.refresh = true;
                        }
                    }
                }
                if self.quitting {
                    self.running = false;
                    return Ok(());
                }
            }
        }
        if self.job.is_none() {
            if self.refresh {
                self.refresh = false;
                self.submit(manager, "status", vec![], "刷新状态");
            } else if self.diagnose {
                self.diagnose = false;
                self.submit(manager, "diagnose", vec![], "检查连接");
            }
        }
        Ok(())
    }
    fn items(&self) -> Vec<Value> {
        match self.page {
            0=>active(&self.state).and_then(|s|s["nodes"].as_array()).map(|nodes| {
                std::iter::once(json!("AUTO")).chain(nodes.iter().filter(|v|!["AUTO","PROXY"].contains(&v.as_str().unwrap_or(""))).cloned())
                    .filter(|v|node_label(v.as_str().unwrap_or("")).to_lowercase().contains(&self.query.to_lowercase())).collect()
            }).unwrap_or_default(),
            1=>MODES.iter().map(|(id,name,description)|json!({"id":id,"name":name,"description":description})).collect(),
            2=>self.state["subscriptions"].as_array().cloned().unwrap_or_default(),
            3=>vec![json!({"id":"diagnose","name":"重新检测"}),json!({"id":"restart","name":"重启核心"}),json!({"id":"stop","name":"停止核心"})],
            _=> {
                let settings = &self.state["maintenance"]["settings"];
                let seconds = settings["update_interval_secs"].as_u64().unwrap_or(0);
                let mut items = vec![
                    json!({"id":"auto_update","name":format!("订阅自动更新：{}", if seconds==0 {"关闭".into()}else{format!("每 {} 分钟",seconds/60)})}),
                    json!({"id":"update_all","name":"立即更新全部订阅"}),
                    json!({"id":"backup_create","name":"立即备份配置"}),
                    json!({"id":"backup_auto","name":format!("更新前自动备份：{}",if settings["backup_before_update"]==false {"关闭"}else{"开启"})}),
                    json!({"id":"backup_keep","name":format!("备份保留份数：{}",settings["backup_keep"].as_u64().unwrap_or(10))}),
                ];
                for name in self.state["maintenance"]["backups"].as_array().into_iter().flatten().filter_map(Value::as_str) {
                    items.push(json!({"id":format!("restore:{name}"),"name":format!("恢复：{name}")}));
                }
                items
            },
        }
    }
    fn selection(&self) -> Option<Value> {
        let items = self.items();
        items
            .get(self.indices[self.page].min(items.len().saturating_sub(1)))
            .cloned()
    }
    fn move_by(&mut self, amount: isize) {
        if self.nav {
            self.page = (self.page as isize + amount).rem_euclid(PAGES.len() as isize) as usize;
        } else {
            self.indices[self.page] = self.indices[self.page]
                .saturating_add_signed(amount)
                .min(self.items().len().saturating_sub(1));
        }
    }
    fn switch(&mut self, manager: &Manager, page: usize) {
        self.page = page;
        self.nav = false;
        if page == 3 {
            if self.job.is_some() {
                self.diagnose = true;
            } else {
                self.submit(manager, "diagnose", vec![], "检查连接");
            }
        }
    }
    fn confirm(&mut self, title: &str, body: &str, action: &str, args: Vec<Value>) {
        if self.job.is_some() {
            self.notice("当前操作仍在进行，请完成后再执行此操作。", false);
            return;
        }
        self.dialog = Some(Dialog::Confirm {
            title: title.into(),
            body: body.into(),
            action: action.into(),
            args,
            yes: false,
        });
    }
    fn stop_dialog(&mut self) {
        self.confirm(
            "停止核心",
            "会断开其他使用核心的终端。停止后请退出菜单并运行 clash off。",
            "stop",
            vec![],
        );
    }
    fn restart_dialog(&mut self) {
        self.confirm(
            "重启核心",
            "会中断正在使用核心的连接，也会影响其他终端。",
            "restart",
            vec![],
        );
    }
    fn form(&mut self, kind: &str, id: &str) {
        if self.job.is_some() {
            self.notice("当前操作仍在进行，请稍后再输入。", false);
            return;
        }
        self.dialog = Some(Dialog::Form {
            kind: kind.into(),
            field: if kind == "add" {
                "name"
            } else if kind == "auto_update" || kind == "backup_keep" {
                kind
            } else if kind == "search" {
                "search"
            } else {
                "url"
            }
            .into(),
            value: if kind == "search" {
                self.query.clone()
            } else {
                String::new()
            },
            name: String::new(),
            id: id.into(),
            error: String::new(),
        });
    }
    fn activate(&mut self, manager: &Manager) {
        if self.nav {
            self.switch(manager, self.page);
            return;
        }
        let Some(item) = self.selection() else {
            self.notice("请按 3 进入订阅管理，再按 a 添加订阅。", false);
            return;
        };
        match self.page {
            0 => {
                self.submit(manager, "select", vec![item], "选择节点");
            }
            1 => {
                self.submit(manager, "set_mode", vec![item["id"].clone()], "设置模式");
            }
            2 => {
                self.submit(manager, "use", vec![item["id"].clone()], "使用订阅");
            }
            4 => match text(&item, "id") {
                "auto_update" => self.form("auto_update", ""),
                "backup_keep" => self.form("backup_keep", ""),
                "backup_auto" => {
                    self.submit(
                        manager,
                        "backup_auto_set",
                        vec![json!(
                            if self.state["maintenance"]["settings"]["backup_before_update"]
                                == false
                            {
                                "on"
                            } else {
                                "off"
                            }
                        )],
                        "设置自动备份",
                    );
                }
                "update_all" | "backup_create" => {
                    self.submit(manager, text(&item, "id"), vec![], text(&item, "name"));
                }
                id if id.starts_with("restore:") => self.confirm(
                    "恢复配置",
                    "将替换当前订阅、节点和模式，并自动备份当前配置。",
                    "backup_restore",
                    vec![json!(&id[8..])],
                ),
                _ => {}
            },
            _ => match text(&item, "id") {
                "restart" => self.restart_dialog(),
                "stop" => self.stop_dialog(),
                _ => {
                    self.submit(manager, "diagnose", vec![], "检查连接");
                }
            },
        }
    }
    fn dialog_key(&mut self, manager: &Manager, key: KeyEvent) {
        let Some(mut dialog) = self.dialog.take() else {
            return;
        };
        if matches!(key.code, K::Esc) || ctrl(key, 'c') {
            self.notice("已取消输入。", false);
            return;
        }
        match &mut dialog {
            Dialog::Help => return,
            Dialog::Confirm {
                title,
                action,
                args,
                yes,
                ..
            } => match key.code {
                K::Tab | K::Left | K::Right | K::Char('h' | 'l') => *yes = !*yes,
                K::Char('y' | 'Y') => {
                    if self.submit(manager, action, args.clone(), title) {
                        return;
                    }
                }
                K::Enter if *yes => {
                    if self.submit(manager, action, args.clone(), title) {
                        return;
                    }
                }
                K::Enter | K::Char('n' | 'N') => {
                    self.notice("已取消操作。", false);
                    return;
                }
                _ => {}
            },
            Dialog::Form {
                kind,
                field,
                value,
                name,
                id,
                error,
            } => {
                if key.code == K::Enter {
                    let input = value.trim().to_owned();
                    if kind == "search" {
                        self.query = input;
                        self.indices[0] = 0;
                        return;
                    }
                    if kind == "auto_update" || kind == "backup_keep" {
                        if self.submit(
                            manager,
                            if kind == "auto_update" {
                                "auto_update_set"
                            } else {
                                "backup_keep"
                            },
                            vec![json!(input)],
                            "保存设置",
                        ) {
                            return;
                        }
                        self.dialog = Some(dialog);
                        return;
                    }
                    if field == "name" {
                        if input.is_empty() {
                            *error = "名称不能为空。".into();
                        } else if input.chars().count() > 60 {
                            *error = "名称最多 60 个字符。".into();
                        } else {
                            *name = input;
                            *field = "url".into();
                            value.clear();
                            error.clear();
                        }
                    } else if input.is_empty() {
                        *error = "订阅地址不能为空。".into();
                    } else {
                        let action = if kind == "add" { "add" } else { "edit" };
                        let args =
                            vec![json!(if action == "add" { name } else { id }), json!(input)];
                        if self.submit(
                            manager,
                            action,
                            args,
                            if action == "add" {
                                "添加订阅"
                            } else {
                                "更新地址"
                            },
                        ) {
                            return;
                        }
                    }
                } else if key.code == K::Backspace {
                    value.pop();
                    error.clear();
                } else if ctrl(key, 'u') {
                    value.clear();
                    error.clear();
                } else if let K::Char(c) = key.code {
                    if !key.modifiers.contains(KeyModifiers::CONTROL)
                        && !c.is_control()
                        && value.chars().count() < if field == "url" { 4096 } else { 120 }
                    {
                        value.push(c);
                        error.clear();
                    }
                }
            }
        }
        self.dialog = Some(dialog);
    }
    pub fn key(&mut self, manager: &Manager, key: KeyEvent) {
        // Crossterm reports Esc followed immediately by a character as Alt+key.
        // Preserve the MVP's Esc-cancel followed by normal navigation behavior.
        if key.modifiers.contains(KeyModifiers::ALT) && matches!(key.code, K::Char(_)) {
            let mut plain = key;
            plain.modifiers.remove(KeyModifiers::ALT);
            self.key(manager, KeyEvent::new(K::Esc, KeyModifiers::NONE));
            self.key(manager, plain);
            return;
        }
        if ctrl(key, 'd') {
            self.dialog = None;
            self.quit();
            return;
        }
        if self.dialog.is_some() {
            self.dialog_key(manager, key);
            return;
        }
        if ctrl(key, 'c') || key.code == K::Esc {
            self.nav = true;
            if let Some(job) = self.job.as_mut() {
                job.cancel();
                self.notice("正在取消，等待当前操作结束；可继续浏览。", false);
            } else {
                self.notice("已返回任务导航。", false);
            }
            return;
        }
        match key.code {
            K::Char('q' | 'Q') => self.quit(),
            K::Char(c @ '1'..='5') => self.switch(manager, (c as u8 - b'1') as usize),
            K::Left => self.nav = true,
            K::Right if self.nav => self.switch(manager, self.page),
            K::Tab => {
                if self.nav {
                    self.switch(manager, self.page);
                } else {
                    self.nav = true;
                }
            }
            K::Up | K::Char('k') => self.move_by(-1),
            K::Down | K::Char('j') => self.move_by(1),
            K::PageDown | K::Char('n') if !self.nav => self.move_by(self.rows as isize),
            K::PageUp | K::Char('p') if !self.nav => self.move_by(-(self.rows as isize)),
            K::Home | K::Char('g') if !self.nav => self.indices[self.page] = 0,
            K::End if !self.nav => self.indices[self.page] = self.items().len().saturating_sub(1),
            K::Enter => self.activate(manager),
            K::Char('/') if self.page == 0 => self.form("search", ""),
            K::Char('t') if self.page == 0 && active(&self.state).is_some() => {
                self.submit(manager, "delays_all", vec![], "全部节点测速");
            }
            K::Char('s') => {
                self.submit(manager, "ensure", vec![], "启动核心");
            }
            K::Char('x') => self.stop_dialog(),
            K::Char('r') => {
                let action = if self.page == 3 { "diagnose" } else { "status" };
                self.submit(
                    manager,
                    action,
                    vec![],
                    if self.page == 3 {
                        "检查连接"
                    } else {
                        "刷新状态"
                    },
                );
            }
            K::Char('R') if self.page == 3 => self.restart_dialog(),
            K::Char('a') if self.page == 2 => self.form("add", ""),
            K::Char(c @ ('u' | 'e' | 'd')) if self.page == 2 => {
                if let Some(item) = self.selection() {
                    match c {
                        'u' => {
                            self.submit(manager, "update", vec![item["id"].clone()], "更新订阅");
                        }
                        'e' => self.form("edit", text(&item, "id")),
                        _ => self.confirm(
                            "删除订阅",
                            &format!("删除“{}”？{}", clean(text(&item, "name")),
                                if item["id"] != self.state["active"] { "将移除此订阅记录。" }
                                else if self.state["subscriptions"].as_array().is_some_and(|s| s.len() == 1) {
                                    "最后一个订阅被删除后，核心将停止。请运行 clash off 清理终端代理。"
                                } else { "将切换到其他订阅。" }),
                            "remove",
                            vec![item["id"].clone()],
                        ),
                    }
                }
            }
            K::Char('?') => self.dialog = Some(Dialog::Help),
            _ => {}
        }
    }
    fn quit(&mut self) {
        if let Some(job) = self.job.as_mut() {
            job.cancel();
            self.quitting = true;
            self.notice("正在取消，等待当前操作结束后退出。", false);
        } else {
            self.running = false;
        }
    }
    pub fn render(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let w = area.width;
        let h = area.height;
        let accent = if std::env::var_os("NO_COLOR").is_some() {
            Style::default()
        } else {
            Style::default().fg(Color::Cyan)
        };
        let bold = Style::default().add_modifier(Modifier::BOLD);
        let reverse = Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD);
        if w < 30 || h < 18 {
            put(frame, 0, 0, "CLASH", w, bold);
            paragraph(
                frame,
                Rect::new(0, 2, w, h.saturating_sub(4)),
                "终端较小，请扩大至至少 30 列、18 行。q 退出。",
                Style::default(),
            );
            return;
        }
        put(frame, 1, 0, "CLASH · 终端工作台", w - 2, bold);
        let status = if self.state["running"] == true {
            "就绪"
        } else {
            "未运行或接口异常"
        };
        put(
            frame,
            1,
            1,
            &format!(
                "核心：{status}   当前终端代理：{}",
                text(&self.state, "shell_proxy")
            ),
            w - 2,
            Style::default(),
        );
        put(
            frame,
            1,
            2,
            &format!(
                "订阅：{}   模式：{}   节点：{}",
                text(&self.state, "name"),
                mode_label(text(&self.state, "mode")),
                node_label(text(&self.state, "node"))
            ),
            w - 2,
            Style::default(),
        );
        line(frame, 0, 3, w);
        let wide = w >= 70;
        let top = if wide { 4 } else { 9 };
        let bottom = h - 5;
        let x = if wide { 20 } else { 1 };
        let width = w - x - 1;
        if wide {
            put(frame, 1, top, "任务", 16, bold);
            for (i, name) in PAGES.iter().enumerate() {
                put(
                    frame,
                    1,
                    top + 2 + i as u16,
                    &format!(
                        "{} {} {name}",
                        if self.page == i {
                            if self.nav {
                                ">"
                            } else {
                                "*"
                            }
                        } else {
                            " "
                        },
                        i + 1
                    ),
                    16,
                    if self.page == i {
                        if self.nav {
                            reverse
                        } else {
                            accent
                        }
                    } else {
                        Style::default()
                    },
                );
            }
            for y in top..=bottom {
                put(frame, 18, y, "│", 1, Style::default());
            }
            if bottom - top >= 11 {
                put(frame, 1, top + 8, "核心操作", 16, bold);
                put(frame, 1, top + 9, "s 启动", 16, Style::default());
                put(frame, 1, top + 10, "x 停止", 16, Style::default());
            }
        } else {
            for (i, name) in PAGES.iter().enumerate() {
                put(
                    frame,
                    1 + (i % 2) as u16 * (w / 2),
                    5 + (i / 2) as u16,
                    &format!("{} {name}", i + 1),
                    w / 2 - 1,
                    if i == self.page {
                        reverse
                    } else {
                        Style::default()
                    },
                );
            }
            line(frame, 0, 8, w);
        }
        let list_start = top + if wide { 3 } else { 2 };
        let detail = if bottom - list_start + 1 >= 7 {
            bottom - 4
        } else {
            bottom + 1
        };
        self.rows = (detail - list_start).clamp(1, 8) as usize;
        let items = self.items();
        self.indices[self.page] = self.indices[self.page].min(items.len().saturating_sub(1));
        let index = self.indices[self.page];
        let offset = index / self.rows * self.rows;
        put(
            frame,
            x,
            top,
            PAGES[self.page],
            width,
            accent.add_modifier(Modifier::BOLD),
        );
        let summary = if self.page == 0 && !self.query.is_empty() {
            format!("搜索：{}", self.query)
        } else {
            format!(
                "共 {} 项   第 {}/{} 页",
                items.len(),
                offset / self.rows + 1,
                items.len().div_ceil(self.rows).max(1)
            )
        };
        put(frame, x, top + 1, &summary, width, Style::default());
        if wide {
            line(frame, x, top + 2, width);
        }
        if items.is_empty() {
            paragraph(
                frame,
                Rect::new(x, list_start, width, self.rows as u16),
                if self.page == 0 && active(&self.state).is_some() {
                    "没有匹配的节点，按 / 修改搜索。"
                } else {
                    "尚未添加订阅，按 3 再按 a 添加。"
                },
                Style::default(),
            );
        }
        for (position, item) in items.iter().skip(offset).take(self.rows).enumerate() {
            let focused = !self.nav && offset + position == index;
            let current = match self.page {
                0 => *item == self.state["node"],
                1 => item["id"] == self.state["mode"],
                2 => item["id"] == self.state["active"],
                _ => false,
            };
            let label = if self.page == 0 {
                node_label(item.as_str().unwrap_or(""))
            } else {
                clean(text(item, "name"))
            };
            let mut label = format!(
                "{} {} {label}",
                if focused { ">" } else { " " },
                if current { "*" } else { " " }
            );
            if self.page == 0 && width >= 42 {
                let left = clip(&label, (width - 14) as usize);
                label = format!(
                    "{left}{}{}",
                    " ".repeat((width as usize - 14).saturating_sub(left.width()).max(1)),
                    delay_label(self.delays.get(item.as_str().unwrap_or("")))
                );
            }
            put(
                frame,
                x,
                list_start + position as u16,
                &label,
                width,
                if focused { reverse } else { Style::default() },
            );
        }
        if detail <= bottom {
            line(frame, x, detail, width);
            put(frame, x, detail, " 所选项目 ", width, bold);
            let selected = self.selection();
            let body = match (self.page, selected) {
                (0, Some(item)) => format!(
                    "{}\n延迟：{}\nEnter 使用  / 搜索  t 全部测速",
                    node_label(item.as_str().unwrap_or("")),
                    delay_label(self.delays.get(item.as_str().unwrap_or("")))
                ),
                (1, Some(item)) => format!(
                    "{}\n{}\n模式只影响接入核心的流量。",
                    text(&item, "name"),
                    text(&item, "description")
                ),
                (2, Some(item)) => format!(
                    "{} · {} 个节点\n更新时间：{}\nEnter 使用  u 更新  e 编辑  d 删除",
                    clean(text(&item, "name")),
                    item["nodes"].as_array().map_or(0, Vec::len),
                    clean(text(&item, "updated"))
                ),
                (3, _) => self.diagnostics.join("\n"),
                (4, _) => format!(
                    "间隔：30m / 6h / 1d，off 关闭\n最近更新：{}\nEnter 设置；选择备份后可确认恢复",
                    self.state["maintenance"]["last_run"]["message"]
                        .as_str()
                        .unwrap_or("暂无记录")
                ),
                _ => "先添加订阅，再选择节点。\n启动核心不会修改当前终端代理。".into(),
            };
            paragraph(
                frame,
                Rect::new(x, detail + 1, width, bottom - detail),
                &body,
                Style::default(),
            );
        }
        let message = if let Some(job) = &self.job {
            if job.cancelled {
                "正在取消，等待安全结束。".into()
            } else {
                format!(
                    "[进行中 {}s] {}；可浏览，q 等待完成后退出",
                    job.started.elapsed().as_secs(),
                    self.job_label
                )
            }
        } else {
            self.message.clone()
        };
        put(
            frame,
            1,
            h - 4,
            &message,
            w - 2,
            if self.error {
                Style::default().fg(Color::Red)
            } else {
                Style::default()
            },
        );
        let context = match self.page {
            0 => "Enter 使用  / 搜索  t 全测",
            1 => "Enter 应用模式",
            2 => "Enter 使用  a 添加  u 更新  e 编辑  d 删除",
            _ => "Enter 执行  r 重新检测  R 重启",
        };
        put(
            frame,
            1,
            h - 3,
            &if self.nav {
                "↑↓/jk 切换任务并预览  →/Tab/Enter 进入内容".into()
            } else {
                format!("←→/Tab 区域  ↑↓/jk 浏览  {context}")
            },
            w - 2,
            Style::default(),
        );
        put(
            frame,
            1,
            h - 2,
            "1-5 任务  s 启动  x 停止  Esc 返回  ? 帮助  q 退出",
            w - 2,
            Style::default(),
        );
        if let Some(dialog) = &self.dialog {
            render_dialog(frame, dialog, accent, reverse);
        }
    }
}

fn ctrl(key: KeyEvent, c: char) -> bool {
    key.code == K::Char(c) && key.modifiers.contains(KeyModifiers::CONTROL)
}
fn put(frame: &mut Frame, x: u16, y: u16, value: &str, width: u16, style: Style) {
    let area = frame.area();
    if y >= area.height || x >= area.width {
        return;
    }
    let width = width.min(area.width - x);
    frame.render_widget(
        Paragraph::new(clip(value, width as usize)).style(style),
        Rect::new(x, y, width, 1),
    );
}
fn paragraph(frame: &mut Frame, area: Rect, value: &str, style: Style) {
    let lines = value.split('\n').map(clean).collect::<Vec<_>>().join("\n");
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .style(style),
        area,
    );
}
fn line(frame: &mut Frame, x: u16, y: u16, width: u16) {
    put(
        frame,
        x,
        y,
        &"─".repeat(width as usize),
        width,
        Style::default(),
    );
}
fn render_dialog(frame: &mut Frame, dialog: &Dialog, accent: Style, reverse: Style) {
    let area = frame.area();
    let width = 58.min(area.width - 4);
    let height = if matches!(dialog, Dialog::Help) {
        12
    } else {
        10
    }
    .min(area.height - 4);
    let rect = Rect::new(
        (area.width - width) / 2,
        (area.height - height) / 2,
        width,
        height,
    );
    frame.render_widget(Clear, rect);
    frame.render_widget(
        Block::default().borders(Borders::TOP | Borders::BOTTOM),
        rect,
    );
    let x = rect.x + 2;
    let y = rect.y;
    let inner = width - 4;
    match dialog {
        Dialog::Help => {
            put(frame, x, y + 1, "帮助", inner, accent);
            paragraph(frame,Rect::new(x,y+2,inner,height-3),"1-5 切换任务；←→/Tab 切换区域\n导航：↑↓/jk 预览，Enter 进入\n列表：↑↓/jk 浏览，Enter 执行\n/ 搜索；t 全部节点测速；5 设置与备份\na 添加，u 更新，e 改地址，d 删除\ns 启动核心；x 停止核心\nq 退出；Esc 返回或关闭\n* 当前使用，> 光标所选\n启动核心后，clash on 启用终端代理",Style::default());
        }
        Dialog::Confirm {
            title, body, yes, ..
        } => {
            put(frame, x, y + 1, title, inner, accent);
            paragraph(
                frame,
                Rect::new(x, y + 3, inner, height - 6),
                body,
                Style::default(),
            );
            put(
                frame,
                x,
                y + height - 3,
                "[取消]",
                10,
                if !yes { reverse } else { Style::default() },
            );
            put(
                frame,
                x + 10,
                y + height - 3,
                "[确认]",
                inner - 10,
                if *yes { reverse } else { Style::default() },
            );
            put(
                frame,
                x,
                y + height - 2,
                "Tab 切换  Enter 执行  Esc 取消",
                inner,
                Style::default(),
            );
        }
        Dialog::Form {
            kind,
            field,
            value,
            error,
            ..
        } => {
            put(
                frame,
                x,
                y + 1,
                if kind == "add" {
                    "添加订阅"
                } else if kind == "edit" {
                    "编辑订阅地址"
                } else if kind == "auto_update" {
                    "自动更新间隔"
                } else if kind == "backup_keep" {
                    "备份保留份数"
                } else {
                    "搜索节点"
                },
                inner,
                accent,
            );
            put(
                frame,
                x,
                y + 3,
                if field == "auto_update" {
                    "输入 30m / 6h / 1d，off 关闭"
                } else if field == "backup_keep" {
                    "输入保留份数：1–100"
                } else if field == "name" {
                    "订阅名称"
                } else if field == "url" {
                    "订阅地址（隐藏）"
                } else {
                    "搜索词（留空显示全部）"
                },
                inner,
                Style::default(),
            );
            let mut displayed = if field == "url" {
                "*".repeat(value.chars().count().min((inner - 2) as usize))
            } else {
                clean(value)
            };
            while displayed.width() > (inner - 2) as usize {
                displayed.remove(0);
            }
            put(frame, x, y + 4, &format!("> {displayed}"), inner, reverse);
            put(
                frame,
                x,
                y + 6,
                error,
                inner,
                Style::default().fg(Color::Red),
            );
            put(
                frame,
                x,
                y + height - 2,
                "Enter 继续  Esc 取消  Ctrl+U 清空",
                inner,
                Style::default(),
            );
        }
    }
}

pub fn run(manager: &Manager) -> Result<()> {
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        bail!("菜单需要交互终端；查看状态请运行 clash status。")
    }
    let closed = Cancellation::default();
    closed.install()?;
    let mut terminal = ratatui::try_init()
        .context("当前终端无法打开工作台，请检查 TERM 或使用 clash menu --plain。")?;
    let result = (|| -> Result<()> {
        let mut app = Workbench::default();
        app.submit(manager, "status", vec![], "读取状态");
        while app.running {
            app.poll(manager)?;
            if closed.0.load(Ordering::Relaxed) {
                app.quit();
            }
            if !app.running {
                break;
            }
            terminal.draw(|frame| app.render(frame))?;
            match event::poll(Duration::from_millis(80)) {
                Ok(true) => match event::read() {
                    Ok(Event::Key(key)) if key.kind != KeyEventKind::Release => {
                        app.key(manager, key)
                    }
                    Ok(_) => {}
                    Err(_) => {
                        app.quit();
                        while app.job.is_some() {
                            app.poll(manager)?;
                            std::thread::sleep(Duration::from_millis(20));
                        }
                        break;
                    }
                },
                Ok(false) => {}
                Err(_) => {
                    app.quit();
                    break;
                }
            }
        }
        Ok(())
    })();
    ratatui::restore();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> Workbench {
        Workbench {
            state: json!({"active":"a","node":"AUTO","mode":"rule","name":"主订阅","shell_proxy":"未开启","subscriptions":[{"id":"a","name":"主订阅","nodes":["香港 01","Tokyo 02"]}]}),
            ..Default::default()
        }
    }
    #[test]
    fn navigation_previews_without_starting_work() {
        let mut app = fixture();
        app.nav = true;
        app.move_by(2);
        assert_eq!(app.page, 2);
        assert!(app.nav);
        assert!(app.job.is_none());
        app.move_by(2);
        assert_eq!(app.page, 4);
    }
    #[test]
    fn search_and_selection_are_bounded() {
        let mut app = fixture();
        app.query = "tokyo".into();
        assert_eq!(app.items(), vec![json!("Tokyo 02")]);
        app.move_by(100);
        assert_eq!(app.selection(), Some(json!("Tokyo 02")));
    }
    #[test]
    fn destructive_confirmation_defaults_to_cancel() {
        let mut app = fixture();
        app.stop_dialog();
        assert!(matches!(
            app.dialog,
            Some(Dialog::Confirm { yes: false, .. })
        ));
    }
    #[test]
    fn renders_wide_narrow_and_small_terminals_without_panics() {
        for (width, height) in [(120, 30), (80, 24), (40, 24), (30, 18), (10, 5)] {
            let backend = ratatui::backend::TestBackend::new(width, height);
            let mut terminal = ratatui::Terminal::new(backend).unwrap();
            let mut app = fixture();
            for page in 0..PAGES.len() {
                app.page = page;
                terminal.draw(|f| app.render(f)).unwrap();
            }
            app.form("add", "");
            if let Some(Dialog::Form { field, value, .. }) = &mut app.dialog {
                *field = "url".into();
                *value = "https://host/?token=PRIVATE".into();
            }
            terminal.draw(|f| app.render(f)).unwrap();
            let output = format!("{:?}", terminal.backend().buffer());
            assert!(!output.contains("PRIVATE"));
        }
    }
}

use crate::lifecycle::{ChildGuard, Core};
use crate::storage::{atomic, digest, private_dir, random_hex, remove_file, Cancellation, Lock};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::fs;
use std::io::Read;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use url::Url;

pub const LIMIT: usize = 10 * 1024 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Subscription {
    pub id: String,
    pub name: String,
    pub url: String,
    pub cache: String,
    pub nodes: Vec<String>,
    pub selected: String,
    pub updated: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct State {
    pub version: u32,
    pub active_id: Option<String>,
    pub mode: String,
    pub secret: String,
    pub subscriptions: Vec<Subscription>,
}

#[derive(Serialize, Deserialize)]
struct Transaction {
    state: State,
    config: Option<String>,
    running: bool,
    had_state: bool,
}

pub struct Manager {
    pub root: PathBuf,
    pub core: Core,
    pub cancel: Cancellation,
    pub api_url: Url,
    pub http_url: Url,
    pub socks_url: Url,
    client: ureq::Agent,
}

fn local_url(value: &str, scheme: &str) -> Result<Url> {
    let url = Url::parse(value).context("代理或控制端口格式无效。")?;
    if url.scheme() != scheme
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || !matches!(url.path(), "" | "/")
        || url.query().is_some()
        || url.fragment().is_some()
    {
        bail!("MVP 仅支持 127.0.0.1 上的代理和控制端口。请检查环境设置。")
    }
    Ok(url)
}

fn agent(timeout: Duration) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(timeout)
        .try_proxy_from_env(false)
        .build()
}

pub fn subscription_url(value: &str) -> Result<Url> {
    if value.chars().any(|c| c <= ' ' || c == '\x7f') {
        bail!("订阅链接不能包含空格或控制字符。")
    }
    let url = Url::parse(value).map_err(|_| anyhow::anyhow!("订阅链接格式有误，请重新粘贴。"))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        bail!("请输入 http:// 或 https:// 订阅链接。")
    }
    Ok(url)
}

impl Manager {
    pub fn new(root: PathBuf) -> Result<Self> {
        Ok(Self {
            core: Core::new(&root),
            root,
            cancel: Cancellation::default(),
            api_url: local_url(
                &std::env::var("MIHOMO_API_URL").unwrap_or_else(|_| "http://127.0.0.1:9090".into()),
                "http",
            )?,
            http_url: local_url(
                &std::env::var("CLASH_HTTP_PROXY")
                    .unwrap_or_else(|_| "http://127.0.0.1:7890".into()),
                "http",
            )?,
            socks_url: local_url(
                &std::env::var("CLASH_SOCKS_PROXY")
                    .unwrap_or_else(|_| "socks5h://127.0.0.1:7891".into()),
                "socks5h",
            )?,
            client: agent(Duration::from_secs(3)),
        })
    }
    fn directory(&self) -> PathBuf {
        self.root.join("runtime/mvp")
    }
    fn state_path(&self) -> PathBuf {
        self.directory().join("state.json")
    }
    fn journal(&self) -> PathBuf {
        self.directory().join("transaction.json")
    }
    pub fn config_path(&self) -> PathBuf {
        self.root.join("conf/config.yaml")
    }

    pub fn load(&self) -> Result<State> {
        if !self.state_path().exists() {
            return Ok(State {
                version: 1,
                active_id: None,
                mode: "rule".into(),
                secret: random_hex(24)?,
                subscriptions: vec![],
            });
        }
        let error = || anyhow::anyhow!("订阅记录损坏，请保留 runtime/mvp 目录并恢复备份。");
        let state: State =
            serde_json::from_slice(&fs::read(self.state_path()).map_err(|_| error())?)
                .map_err(|_| error())?;
        let ids: HashSet<_> = state.subscriptions.iter().map(|s| &s.id).collect();
        if state.version != 1
            || !matches!(state.mode.as_str(), "rule" | "global" | "direct")
            || state.secret.is_empty()
            || state.secret.chars().any(char::is_control)
            || ids.len() != state.subscriptions.len()
            || state.active_id.as_ref().is_some_and(|id| !ids.contains(id))
        {
            return Err(error());
        }
        Ok(state)
    }
    fn save(&self, state: &State) -> Result<()> {
        atomic(&self.state_path(), serde_json::to_vec_pretty(state)?)
    }
    fn item<'a>(&self, state: &'a State, id: Option<&str>) -> Result<&'a Subscription> {
        state
            .subscriptions
            .iter()
            .find(|s| Some(s.id.as_str()) == id.or(state.active_id.as_deref()))
            .context("还没有可用订阅，请在 clash 菜单中添加。")
    }
    fn api_at(
        &self,
        base: &str,
        secret: &str,
        path: &str,
        method: &str,
        data: Option<Value>,
    ) -> Result<Value> {
        let request = self
            .client
            .request(method, &format!("{}{path}", base.trim_end_matches('/')))
            .set("Authorization", &format!("Bearer {secret}"))
            .set("Content-Type", "application/json");
        let response = match data {
            Some(data) => request.send_json(data),
            None => request.call(),
        };
        let response = response.map_err(|error| match error {
            ureq::Error::Status(code, _) => {
                anyhow::anyhow!("控制接口操作失败（HTTP {code}），请检查核心状态。")
            }
            _ => anyhow::anyhow!("控制接口连接失败，请检查核心状态。"),
        })?;
        let mut body = Vec::new();
        response
            .into_reader()
            .take((LIMIT + 1) as u64)
            .read_to_end(&mut body)
            .context("控制接口读取失败。")?;
        if body.len() > LIMIT {
            bail!("控制接口返回内容过大。")
        }
        if body.is_empty() {
            return Ok(json!({}));
        }
        serde_json::from_slice(&body).context("控制接口返回格式异常。")
    }
    pub fn api(
        &self,
        state: &State,
        path: &str,
        method: &str,
        data: Option<Value>,
    ) -> Result<Value> {
        self.api_at(self.api_url.as_str(), &state.secret, path, method, data)
    }
    pub fn healthy(&self, state: &State) -> bool {
        self.core.running() && self.api(state, "/version", "GET", None).is_ok()
    }
    fn fetch(&self, url: &str) -> Result<Vec<u8>> {
        let parsed = subscription_url(url)?;
        self.cancel.checkpoint()?;
        let response = crate::http::external_agent(&parsed, Duration::from_secs(30))?
            .get(url)
            .set("User-Agent", "clash-linux-mvp/1.0")
            .call()
            .map_err(|_| anyhow::anyhow!("订阅下载失败，请检查链接和网络；原订阅未修改。"))?;
        subscription_url(response.get_url())?;
        let mut body = Vec::new();
        let mut reader = response.into_reader().take((LIMIT + 1) as u64);
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            self.cancel.checkpoint()?;
            if Instant::now() >= deadline {
                bail!("订阅下载超时，原订阅未修改。")
            }
            let mut buffer = [0; 65536];
            let read = reader.read(&mut buffer);
            // A signal may interrupt a blocking socket read before the next loop.
            self.cancel.checkpoint()?;
            let size = read.map_err(|_| anyhow::anyhow!("订阅下载失败；原订阅未修改。"))?;
            if size == 0 {
                break;
            }
            body.extend_from_slice(&buffer[..size]);
        }
        self.cancel.checkpoint()?;
        if body.is_empty() || body.len() > LIMIT {
            bail!("订阅为空或超过 10 MB，请检查链接。")
        }
        Ok(body)
    }
    fn validate_provider(&self, body: &[u8]) -> Result<Vec<String>> {
        let base = tempfile::tempdir()?;
        atomic(&base.path().join("nodes.yaml"), body)?;
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        let port = listener.local_addr()?.port();
        drop(listener);
        let secret = random_hex(24)?;
        let config = json!({"external-controller":format!("127.0.0.1:{port}"),"secret":secret,
            "allow-lan":false,"log-level":"silent","ipv6":false,
            "proxy-providers":{"subscription":{"type":"file","path":"./nodes.yaml","health-check":{"enable":false}}},
            "proxy-groups":[{"name":"CHECK","type":"select","use":["subscription"]}],"rules":["MATCH,CHECK"]});
        let path = base.path().join("config.yaml");
        atomic(&path, serde_json::to_vec(&config)?)?;
        let mut child = ChildGuard(
            Command::new(self.core.require_binary()?)
                .arg("-d")
                .arg(base.path())
                .arg("-f")
                .arg(&path)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .context("无法启动订阅校验核心，请检查安装。")?,
        );
        let deadline = Instant::now() + Duration::from_secs(10);
        while child.0.try_wait()?.is_none() && Instant::now() < deadline {
            self.cancel.checkpoint()?;
            if let Ok(payload) = self.api_at(
                &format!("http://127.0.0.1:{port}"),
                &secret,
                "/providers/proxies/subscription",
                "GET",
                None,
            ) {
                if let Some(proxies) = payload["proxies"].as_array() {
                    let names: Vec<String> = proxies
                        .iter()
                        .filter_map(|p| p["name"].as_str().map(str::to_owned))
                        .collect();
                    if !names.is_empty() {
                        if names.len() != names.iter().collect::<HashSet<_>>().len()
                            || names.iter().any(|n| {
                                ["AUTO", "PROXY", "GLOBAL", "DIRECT", "REJECT", "CHECK"]
                                    .contains(&n.as_str())
                            })
                        {
                            bail!("节点名称重复或使用保留名称（AUTO / PROXY 等），请更换订阅格式。")
                        }
                        return Ok(names);
                    }
                }
            }
            thread::sleep(Duration::from_millis(100));
        }
        bail!("订阅没有可用节点，或格式不受支持。需要 Clash/Mihomo YAML 格式。")
    }
    fn config_text(&self, state: &State) -> Result<Vec<u8>> {
        let item = self.item(state, None)?;
        let relative = Path::new(&item.cache);
        if relative.is_absolute()
            || relative
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
        {
            bail!("订阅缓存路径无效，请恢复备份。")
        }
        let cache = self.root.join("conf").join(relative);
        let body = fs::read(&cache).context("订阅缓存缺失或损坏，请在订阅菜单中更新此订阅。")?;
        if cache.file_stem().and_then(|s| s.to_str()) != Some(&digest(&body)) {
            bail!("订阅缓存缺失或损坏，请在订阅菜单中更新此订阅。")
        }
        Ok(serde_json::to_vec_pretty(&json!({
            "port":self.http_url.port(),"socks-port":self.socks_url.port(),"allow-lan":false,"bind-address":"127.0.0.1",
            "mode":state.mode,"log-level":"warning","ipv6":true,
            "external-controller":format!("127.0.0.1:{}",self.api_url.port().unwrap()),"secret":state.secret,
            "profile":{"store-selected":false},
            "proxy-providers":{"subscription":{"type":"file","path":item.cache,"health-check":{"enable":false}}},
            "proxy-groups":[{"name":"PROXY","type":"select","proxies":["AUTO"],"use":["subscription"]},
                {"name":"AUTO","type":"url-test","use":["subscription"],"url":"https://www.gstatic.com/generate_204","interval":600,"lazy":true}],
            "rules":["DOMAIN,localhost,DIRECT","IP-CIDR,127.0.0.0/8,DIRECT,no-resolve","IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
                "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve","IP-CIDR,192.168.0.0/16,DIRECT,no-resolve","IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
                "IP-CIDR6,::1/128,DIRECT,no-resolve","IP-CIDR6,fc00::/7,DIRECT,no-resolve","IP-CIDR6,fe80::/10,DIRECT,no-resolve","MATCH,PROXY"]
        }))?)
    }
    fn restore_selection(&self, state: &State) -> Result<()> {
        let item = self.item(state, None)?;
        let expected: HashSet<_> = item.nodes.iter().map(String::as_str).collect();
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            if let Ok(payload) = self.api(state, "/providers/proxies/subscription", "GET", None) {
                if let Some(proxies) = payload["proxies"].as_array() {
                    let actual: HashSet<_> =
                        proxies.iter().filter_map(|p| p["name"].as_str()).collect();
                    if actual == expected {
                        break;
                    }
                }
            }
            if Instant::now() >= deadline {
                bail!("节点未正确加载，配置应用失败。")
            }
            thread::sleep(Duration::from_millis(100));
        }
        self.api(
            state,
            "/proxies/PROXY",
            "PUT",
            Some(json!({"name":item.selected})),
        )?;
        self.api(
            state,
            "/proxies/GLOBAL",
            "PUT",
            Some(json!({"name":"PROXY"})),
        )?;
        Ok(())
    }
    fn reload(&self, state: &State) -> Result<()> {
        self.api(
            state,
            "/configs?force=true",
            "PUT",
            Some(json!({"path":self.config_path()})),
        )?;
        self.restore_selection(state)
    }
    pub fn recover(&self) -> Result<()> {
        if !self.journal().exists() {
            return Ok(());
        }
        let record: Transaction = serde_json::from_slice(&fs::read(self.journal())?)
            .context("事务记录损坏，请保留 runtime/mvp 目录并恢复备份。")?;
        match record.config {
            Some(config) => atomic(&self.config_path(), config)?,
            None => remove_file(&self.config_path())?,
        }
        if record.running {
            if !self.healthy(&record.state) {
                if self.core.running() {
                    self.core.stop()?;
                }
                self.core.start(|| self.healthy(&record.state))?;
            }
            self.reload(&record.state)?;
        }
        if record.had_state {
            self.save(&record.state)?;
        } else {
            remove_file(&self.state_path())?;
        }
        remove_file(&self.journal())
    }
    pub(crate) fn apply(&self, old: &State, new: &State) -> Result<()> {
        let candidate = self.root.join("conf/.mvp-candidate.yaml");
        let empty = new.active_id.is_none();
        if !empty {
            atomic(&candidate, self.config_text(new)?)?;
        }
        let result = (|| -> Result<()> {
            if !empty {
                self.core.validate(&candidate)?;
            }
            let running = self.core.running();
            if running && !empty && !self.healthy(old) {
                bail!("核心正在运行，但接口不可用。请先诊断，原配置未修改。")
            }
            let record = Transaction {
                state: old.clone(),
                config: if self.config_path().exists() {
                    Some(fs::read_to_string(self.config_path())?)
                } else {
                    None
                },
                running,
                had_state: self.state_path().exists(),
            };
            self.cancel.checkpoint()?;
            atomic(&self.journal(), serde_json::to_vec(&record)?)?;
            // After the journal is durable, finish or recover without cancellation.
            let commit = (|| -> Result<()> {
                if empty {
                    self.core.stop()?;
                    remove_file(&self.config_path())?;
                } else {
                    atomic(&self.config_path(), fs::read(&candidate)?)?;
                }
                if running && !empty {
                    // Mihomo's reload endpoint does not replace controller auth.
                    if old.secret != new.secret {
                        self.core.stop()?;
                        self.core.start(|| self.healthy(new))?;
                    } else {
                        self.api(
                            old,
                            "/configs?force=true",
                            "PUT",
                            Some(json!({"path":self.config_path()})),
                        )?;
                    }
                    self.restore_selection(new)?;
                }
                self.save(new)?;
                remove_file(&self.journal())
            })();
            if commit.is_err() && self.recover().is_err() {
                bail!("切换失败，回退尚未完成。旧记录已保留，请检查核心后重试。")
            }
            commit
        })();
        let cleanup = remove_file(&candidate);
        result.and(cleanup)
    }
    fn prepare(
        &self,
        state: &State,
        name: &str,
        url: &str,
        id: Option<&str>,
    ) -> Result<Subscription> {
        let name = name.trim();
        let url = url.trim();
        if name.is_empty() || name.chars().count() > 60 || name.chars().any(char::is_control) {
            bail!("名称需要 1–60 个字符，不能包含控制字符。")
        }
        for item in &state.subscriptions {
            if Some(item.id.as_str()) != id {
                if item.url == url {
                    bail!("这个订阅链接已经保存。")
                }
                if item.name == name {
                    bail!("名称已经使用，请换一个名称。")
                }
            }
        }
        let body = self.fetch(url)?;
        let nodes = self.validate_provider(&body)?;
        self.cancel.checkpoint()?;
        let id = match id {
            Some(id) => id.to_owned(),
            None => random_hex(16)?,
        };
        if id.len() > 128 || !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
            bail!("订阅标识无效。")
        }
        let relative = format!("mvp-providers/{id}/{}.yaml", digest(&body));
        atomic(&self.root.join("conf").join(&relative), body)?;
        let updated = timestamp();
        Ok(Subscription {
            id,
            name: name.into(),
            url: url.into(),
            cache: format!("./{relative}"),
            nodes,
            selected: "AUTO".into(),
            updated,
        })
    }
    fn check_ports(&self) -> Result<()> {
        let ports = [
            self.http_url.port().unwrap(),
            self.socks_url.port().unwrap(),
            self.api_url.port().unwrap(),
        ];
        if ports.iter().collect::<HashSet<_>>().len() != ports.len() {
            bail!("HTTP、SOCKS 和控制接口必须使用不同端口。")
        }
        let mut listeners = Vec::new();
        for port in ports {
            listeners.push(TcpListener::bind(("127.0.0.1", port)).map_err(|_| {
                anyhow::anyhow!("端口 {port} 已被占用，请先停止占用该端口的程序。")
            })?);
        }
        Ok(())
    }
    pub fn status(&self) -> Result<Value> {
        let state = self.load()?;
        let active = self.item(&state, None).ok();
        let running = self.healthy(&state);
        let core_state = if running {
            "running"
        } else if self.core.running() {
            "unhealthy"
        } else {
            "stopped"
        };
        let keys = [
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "ALL_PROXY",
        ];
        let actual: Vec<_> = keys
            .iter()
            .map(|key| std::env::var(key).unwrap_or_default())
            .collect();
        let expected: Vec<_> = (0..6)
            .map(|i| {
                if i < 4 {
                    self.http_url.as_str().trim_end_matches('/')
                } else {
                    self.socks_url.as_str().trim_end_matches('/')
                }
            })
            .collect();
        let shell = if actual.iter().map(String::as_str).eq(expected) {
            if running {
                "已开启"
            } else {
                "已设置，但核心未就绪"
            }
        } else if actual.iter().all(String::is_empty) {
            "未开启"
        } else {
            "部分设置或其他代理"
        };
        let subscriptions: Vec<Value> = state.subscriptions.iter().map(|s| json!({"id":s.id,"name":s.name,"nodes":s.nodes,"selected":s.selected,"updated":s.updated})).collect();
        Ok(
            json!({"running":running,"core_state":core_state,"active":state.active_id,
            "name":active.map(|s|s.name.as_str()).unwrap_or("未添加"),"mode":state.mode,
            "node":active.map(|s|s.selected.as_str()).unwrap_or("AUTO"),"shell_proxy":shell,"subscriptions":subscriptions,
            "maintenance":crate::maintenance::status(self)?}),
        )
    }
    fn ensure(&self) -> Result<Value> {
        let state = self.load()?;
        self.item(&state, None)?;
        if self.healthy(&state) {
            return Ok(json!("核心已就绪。"));
        }
        if self.core.running() {
            bail!("核心正在运行但接口异常，请在菜单中诊断。")
        }
        self.check_ports()?;
        let config = self.config_text(&state)?;
        let candidate = self.root.join("conf/.mvp-start.yaml");
        atomic(&candidate, &config)?;
        let validation = self.core.validate(&candidate);
        remove_file(&candidate)?;
        validation?;
        self.cancel.checkpoint()?;
        atomic(&self.config_path(), config)?;
        self.core.start(|| self.healthy(&state))?;
        if let Err(error) = self.restore_selection(&state) {
            self.core.stop()?;
            return Err(error);
        }
        Ok(json!("核心已启动。"))
    }
    fn delays(&self, nodes: &[String]) -> Result<Value> {
        let state = self.load()?;
        if !self.healthy(&state) {
            bail!("请先启动核心（s 或 clash start），再测试延迟。")
        }
        let mut result = serde_json::Map::new();
        for node in nodes {
            self.cancel.checkpoint()?;
            let encoded: String = url::form_urlencoded::byte_serialize(node.as_bytes())
                .collect::<String>()
                .replace('+', "%20");
            let path = if node == "AUTO" {
                "/proxies/AUTO/delay".into()
            } else {
                format!("/providers/proxies/subscription/{encoded}/healthcheck")
            };
            let response = self.api(
                &state,
                &format!("{path}?timeout=2000&url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204"),
                "GET",
                None,
            );
            result.insert(
                node.clone(),
                response
                    .ok()
                    .and_then(|r| r.get("delay").cloned())
                    .unwrap_or(json!("超时/失败")),
            );
        }
        self.cancel.checkpoint()?;
        Ok(Value::Object(result))
    }
    fn diagnose(&self) -> Result<Value> {
        self.cancel.checkpoint()?;
        let status = self.status()?;
        let mut lines = vec![
            format!(
                "核心接口：{}",
                if status["running"] == true {
                    "正常"
                } else {
                    "未运行或异常"
                }
            ),
            format!(
                "当前终端代理：{}",
                status["shell_proxy"].as_str().unwrap_or("未知")
            ),
        ];
        if self.journal().exists() {
            lines.push("上次操作中断：下一次修改将尝试回退。".into());
        }
        if status["running"] == true {
            let proxy = ureq::Proxy::new(self.http_url.as_str()).context("代理地址无效。")?;
            let client = ureq::AgentBuilder::new()
                .timeout(Duration::from_secs(8))
                .proxy(proxy)
                .build();
            let message = match client.get("http://www.gstatic.com/generate_204").call() {
                Ok(r) if r.status() == 204 => "正常",
                Ok(_) => "返回非预期状态",
                Err(_) => "失败；尝试切换节点或更新订阅。",
            };
            lines.push(format!("代理出站：{message}"));
        } else {
            lines.push("下一步：按 s 或运行 clash start；失败时检查 logs/mihomo.log。".into());
        }
        self.cancel.checkpoint()?;
        Ok(json!(lines))
    }
    pub fn perform(&self, action: &str, args: &[Value]) -> Result<Value> {
        validate_request(action, args)?;
        self.cancel.checkpoint()?;
        if matches!(action, "update_all" | "update_due") {
            return crate::maintenance::update(self, action == "update_due");
        }
        if action == "backup_list" {
            return Ok(json!(crate::backup::list(self)?));
        }
        if matches!(action, "status" | "diagnose" | "delays" | "delays_all") {
            return match action {
                "status" => self.status(),
                "diagnose" => self.diagnose(),
                "delays" => self.delays(&serde_json::from_value::<Vec<String>>(args[0].clone())?),
                _ => {
                    let state = self.load()?;
                    let mut nodes = vec!["AUTO".to_owned()];
                    nodes.extend(
                        self.item(&state, None)?
                            .nodes
                            .iter()
                            .filter(|n| !["AUTO", "PROXY"].contains(&n.as_str()))
                            .cloned(),
                    );
                    self.delays(&nodes)
                }
            };
        }
        private_dir(&self.directory())?;
        let _mutation = Lock::acquire(&self.directory().join("lock"), true)?;
        self.recover()?;
        self.cancel.checkpoint()?;
        let arg = |index: usize| args[index].as_str().unwrap();
        match action {
            "backup_create" => return Ok(json!(crate::backup::create(self)?)),
            "backup_restore" => return crate::backup::restore(self, arg(0)),
            "auto_update_set" => return crate::maintenance::configure(self, arg(0)),
            "backup_auto_set" | "backup_keep" => {
                return crate::maintenance::configure_backup(self, action, arg(0))
            }
            _ => {}
        }
        if action == "ensure" {
            return self.ensure();
        }
        if action == "stop" {
            self.core.stop()?;
            return Ok(json!("核心已停止，所有依赖该核心的代理连接已断开。"));
        }
        if action == "restart" {
            let state = self.load()?;
            let config = self.config_text(&state)?;
            let candidate = self.root.join("conf/.mvp-restart.yaml");
            atomic(&candidate, &config)?;
            let result = self.core.validate(&candidate);
            remove_file(&candidate)?;
            result?;
            self.cancel.checkpoint()?;
            self.core.stop()?;
            self.ensure()?;
            return Ok(json!("核心已重启。"));
        }
        let old = self.load()?;
        let mut new = old.clone();
        let message = match action {
            "add" => {
                if old.subscriptions.is_empty() && self.config_path().exists() {
                    bail!("检测到旧配置。请先备份并移走 conf/config.yaml，再使用新订阅菜单。")
                }
                let item = self.prepare(&new, arg(0), arg(1), None)?;
                if new.active_id.is_none() {
                    new.active_id = Some(item.id.clone());
                }
                new.subscriptions.push(item);
                if old.active_id.is_none() {
                    "已添加并设为当前订阅。退出菜单后运行 clash on。"
                } else {
                    "已添加，当前订阅未改变。"
                }
            }
            "use" => {
                self.item(&old, Some(arg(0)))?;
                if old.active_id.as_deref() == Some(arg(0)) {
                    return Ok(json!("已经在使用这个订阅。"));
                }
                new.active_id = Some(arg(0).into());
                "已切换订阅，当前终端开关保持不变。"
            }
            "update" | "edit" => {
                let item = self.item(&old, Some(arg(0)))?;
                let mut replacement = self.prepare(
                    &old,
                    &item.name,
                    if action == "edit" { arg(1) } else { &item.url },
                    Some(&item.id),
                )?;
                let missing =
                    item.selected != "AUTO" && !replacement.nodes.contains(&item.selected);
                if !missing {
                    replacement.selected = item.selected.clone();
                }
                let index = new
                    .subscriptions
                    .iter()
                    .position(|s| s.id == item.id)
                    .unwrap();
                new.subscriptions[index] = replacement;
                if missing {
                    "订阅已更新。原节点已消失，已恢复自动选择。"
                } else {
                    "订阅已更新。"
                }
            }
            "remove" => {
                self.item(&old, Some(arg(0)))?;
                new.subscriptions.retain(|s| s.id != arg(0));
                if old.active_id.as_deref() == Some(arg(0)) {
                    new.active_id = new.subscriptions.first().map(|s| s.id.clone());
                    if new.active_id.is_none() {
                        "最后一个订阅已删除，核心已停止。当前终端如已开启代理，请运行 clash off。"
                    } else {
                        "订阅已删除，已切换到其他订阅。"
                    }
                } else {
                    "订阅记录已删除。"
                }
            }
            "select" => {
                let item = self.item(&old, None)?;
                if arg(0) != "AUTO" && !item.nodes.iter().any(|n| n == arg(0)) {
                    bail!("节点不存在，请重新打开列表。")
                }
                new.subscriptions
                    .iter_mut()
                    .find(|s| s.id == item.id)
                    .unwrap()
                    .selected = arg(0).into();
                if self.core.running() {
                    "节点已保存。"
                } else {
                    "节点已保存。下次 clash start 或 clash on 时生效。"
                }
            }
            "set_mode" => {
                if !["rule", "global", "direct"].contains(&arg(0)) {
                    bail!("模式只能是 rule / global / direct。")
                }
                new.mode = arg(0).into();
                "模式已保存。"
            }
            _ => bail!("无效的操作请求。"),
        };
        let affects_active = match action {
            "add" => old.active_id.is_none(),
            "update" | "edit" => old.active_id.as_deref() == Some(arg(0)),
            "remove" => old.active_id.as_deref() == Some(arg(0)),
            _ => true,
        };
        if affects_active {
            self.apply(&old, &new)?;
        } else {
            self.cancel.checkpoint()?;
            self.save(&new)?;
        }
        Ok(json!(message))
    }
}

pub fn validate_request(action: &str, args: &[Value]) -> Result<()> {
    let count = match action {
        "status" | "diagnose" | "ensure" | "restart" | "stop" | "delays_all" | "update_all"
        | "update_due" | "backup_create" | "backup_list" => 0,
        "use" | "update" | "remove" | "select" | "set_mode" | "delays" | "backup_restore"
        | "auto_update_set" | "backup_auto_set" | "backup_keep" => 1,
        "add" | "edit" => 2,
        _ => bail!("无效的操作请求。"),
    };
    if args.len() != count {
        bail!("无效的操作请求。")
    }
    if action == "delays" {
        if !args[0].as_array().is_some_and(|list| {
            list.len() <= 100
                && list
                    .iter()
                    .all(|v| v.as_str().is_some_and(|s| s.chars().count() <= 4096))
        }) {
            bail!("无效的节点列表。")
        }
    } else if !args
        .iter()
        .all(|v| v.as_str().is_some_and(|s| s.chars().count() <= 16384))
    {
        bail!("无效的操作参数。")
    }
    Ok(())
}

fn timestamp() -> String {
    let now = unsafe { libc::time(std::ptr::null_mut()) };
    let mut tm = std::mem::MaybeUninit::<libc::tm>::uninit();
    let mut buffer = [0u8; 32];
    // localtime_r initializes tm; strftime is bounded by the buffer length.
    unsafe {
        if libc::localtime_r(&now, tm.as_mut_ptr()).is_null() {
            return "未知".into();
        }
        let len = libc::strftime(
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            c"%Y-%m-%d %H:%M".as_ptr(),
            tm.as_ptr(),
        );
        String::from_utf8_lossy(&buffer[..len]).into_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_remote_controller_and_malformed_subscription_urls() {
        for url in [
            "http://0.0.0.0:9090",
            "http://user:pass@127.0.0.1:9090",
            "http://127.0.0.1:9090/?x=1",
            "https://127.0.0.1:9090",
        ] {
            assert!(local_url(url, "http").is_err(), "{url}");
        }
        for url in [
            "https://[bad",
            "https://host:bad",
            "file:///etc/passwd",
            "https://host/\nsecret",
            "https://user:pass@host",
        ] {
            assert!(subscription_url(url).is_err());
        }
    }
    #[test]
    fn worker_action_and_argument_limits() {
        assert!(validate_request("_load", &[]).is_err());
        assert!(validate_request("add", &[json!("one")]).is_err());
        assert!(validate_request("use", &[Value::Null]).is_err());
        assert!(validate_request("delays", &[json!(vec!["node"; 101])]).is_err());
        assert!(validate_request("delays_all", &[]).is_ok());
    }
    #[test]
    fn state_roundtrip_and_status_redact_credentials() {
        let root = tempfile::tempdir().unwrap();
        let manager = Manager::new(root.path().into()).unwrap();
        let mut state = manager.load().unwrap();
        state.subscriptions.push(Subscription {
            id: "one".into(),
            name: "订阅".into(),
            url: "https://host/?token=PRIVATE".into(),
            cache: "./private.yaml".into(),
            nodes: vec!["香港".into()],
            selected: "AUTO".into(),
            updated: "now".into(),
        });
        state.active_id = Some("one".into());
        manager.save(&state).unwrap();
        assert_eq!(manager.load().unwrap().secret, state.secret);
        let status = manager.status().unwrap().to_string();
        assert!(!status.contains("PRIVATE"));
        assert!(!status.contains(&state.secret));
        assert!(!status.contains("private.yaml"));
    }
    #[test]
    fn corrupted_cache_and_state_are_rejected() {
        let root = tempfile::tempdir().unwrap();
        let manager = Manager::new(root.path().into()).unwrap();
        atomic(&manager.state_path(), b"{}").unwrap();
        assert!(manager.load().is_err());
        let state = State {
            version: 1,
            active_id: Some("one".into()),
            mode: "rule".into(),
            secret: "secret".into(),
            subscriptions: vec![Subscription {
                id: "one".into(),
                name: "one".into(),
                url: "http://example.com".into(),
                cache: "../../escape".into(),
                nodes: vec![],
                selected: "AUTO".into(),
                updated: "now".into(),
            }],
        };
        assert!(manager.config_text(&state).is_err());
    }
}

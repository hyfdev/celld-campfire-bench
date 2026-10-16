use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fmt::Write;

pub const ROOMS: u32 = 50;
pub const CACHE_BYTES_PER_ROOM: usize = 1024 * 1024;

#[derive(Clone, Serialize, Deserialize)]
pub enum Value {
    Integer(i64),
    Text(String),
}

pub type Rows = Vec<Vec<Value>>;

impl Value {
    pub fn integer(&self) -> i64 {
        match self {
            Self::Integer(n) => *n,
            Self::Text(_) => panic!("expected integer SQL column"),
        }
    }

    pub fn text(&self) -> &str {
        match self {
            Self::Text(s) => s,
            Self::Integer(_) => panic!("expected text SQL column"),
        }
    }

    fn into_text(self) -> String {
        match self {
            Self::Text(s) => s,
            Self::Integer(_) => panic!("expected text SQL column"),
        }
    }
}

pub trait Host {
    fn accept_socket(&mut self, token: &str) -> Result<u64, String>;
    fn broadcast(&mut self, data: &[u8]) -> Result<(), String>;
    fn query(&mut self, room: u32, sql: &str, params: &[Value]) -> Result<Rows, String>;
}

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub authorization: String,
    pub upgrade: bool,
    pub cache: bool,
    #[serde(with = "serde_bytes")]
    pub body: Vec<u8>,
}

impl Request {
    pub fn route(&self) -> Option<(u32, bool)> {
        let parts: Vec<&str> = self.path.split('/').collect();
        if parts.first() != Some(&"") || parts.get(1) != Some(&"rooms") {
            return None;
        }
        let digits = parts.get(2)?;
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        let room = digits.parse::<u32>().ok()?;
        if !(1..=ROOMS).contains(&room) {
            return None;
        }
        let json = parts.get(3) == Some(&"messages") && parts.len() == 4;
        if !(json || parts.len() == 3) || !(self.method == "GET" || (json && self.method == "POST"))
        {
            return None;
        }
        Some((room, json))
    }
}

#[derive(Serialize, Deserialize)]
pub struct Response {
    pub status: u16,
    pub content_type: String,
    #[serde(with = "serde_bytes")]
    pub body: Vec<u8>,
    pub version: i64,
    pub cache_hit: bool,
    pub sql_calls: u32,
}

impl Response {
    pub fn error(status: u16, message: &str, sql_calls: u32) -> Self {
        Self {
            status,
            content_type: "application/json".into(),
            body: serde_json::to_vec(&serde_json::json!({"error": message})).unwrap(),
            version: 0,
            cache_hit: false,
            sql_calls,
        }
    }
}

#[derive(Deserialize)]
struct Sql {
    auth: String,
    member: String,
    version: String,
    messages: String,
    insert: String,
}

#[derive(Default)]
struct RoomCache {
    version: i64,
    bytes: usize,
    entries: HashMap<(i64, bool), Vec<u8>>,
}

impl RoomCache {
    fn reset(&mut self, version: i64) {
        self.version = version;
        self.entries.clear();
        self.bytes = 0;
    }
}

#[derive(Serialize)]
struct Message {
    id: i64,
    user_id: i64,
    user_name: String,
    body: String,
    created_at: i64,
}

#[derive(Serialize)]
struct Page {
    room_id: u32,
    version: i64,
    unread: i64,
    messages: Vec<Message>,
}

#[derive(Deserialize)]
struct NewMessage {
    body: String,
}

#[derive(Serialize)]
struct Posted<'a> {
    id: i64,
    room_id: u32,
    user_id: i64,
    body: &'a str,
    created_at: i64,
    version: i64,
}

pub struct App {
    sql: Sql,
    rooms: HashMap<u32, RoomCache>,
    initialized: std::collections::HashSet<u32>,
}

fn escaped(out: &mut String, value: &str) {
    for ch in value.chars() {
        out.push_str(match ch {
            '&' => "&amp;",
            '<' => "&lt;",
            '>' => "&gt;",
            '"' => "&quot;",
            '\'' => "&#39;",
            _ => {
                out.push(ch);
                continue;
            }
        });
    }
}

fn html(page: &Page, user: &str) -> Vec<u8> {
    let mut out = String::with_capacity(32768);
    write!(out, "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Room {}</title></head><body><header><h1>Room {}</h1><p>User: ", page.room_id, page.room_id).unwrap();
    escaped(&mut out, user);
    write!(
        out,
        "; unread: {}; version: {}</p></header><main>",
        page.unread, page.version
    )
    .unwrap();
    for message in &page.messages {
        write!(out, "<article data-id=\"{}\"><h2>", message.id).unwrap();
        escaped(&mut out, &message.user_name);
        write!(out, "</h2><time>{}</time><p>", message.created_at).unwrap();
        escaped(&mut out, &message.body);
        out.push_str("</p></article>");
    }
    out.push_str("</main><form method=\"post\" action=\"/rooms/");
    write!(out, "{}/messages\"><textarea name=\"body\"></textarea><button>Send</button></form></body></html>", page.room_id).unwrap();
    out.into_bytes()
}

impl App {
    pub fn new() -> Self {
        Self {
            sql: serde_json::from_str(include_str!("../sql.json")).unwrap(),
            rooms: HashMap::new(),
            initialized: std::collections::HashSet::new(),
        }
    }

    fn render_or_post(
        &mut self,
        host: &mut impl Host,
        request: Request,
    ) -> Result<Response, String> {
        let Some((room, json)) = request.route() else {
            return Ok(Response::error(404, "not found", 0));
        };
        let Some(token) = request.authorization.strip_prefix("Bearer ") else {
            return Ok(Response::error(401, "unauthorized", 0));
        };
        let mut calls = 1;
        let users = host.query(room, &self.sql.auth, &[Value::Text(token.into())])?;
        let Some(user) = users.first() else {
            return Ok(Response::error(401, "unauthorized", calls));
        };
        let user_id = user[0].integer();
        let user_name = user[1].text().to_owned();
        calls += 1;
        let membership = host.query(room, &self.sql.member, &[Value::Integer(user_id)])?;
        let Some(member) = membership.first() else {
            return Ok(Response::error(403, "forbidden", calls));
        };
        let unread = member[0].integer();

        if request.method == "POST" {
            let Ok(message) = serde_json::from_slice::<NewMessage>(&request.body) else {
                return Ok(Response::error(400, "invalid body", calls));
            };
            if message.body.trim().is_empty() || message.body.len() > 8192 {
                return Ok(Response::error(400, "invalid body", calls));
            }
            calls += 1;
            let inserted = host.query(
                room,
                &self.sql.insert,
                &[Value::Integer(user_id), Value::Text(message.body.clone())],
            )?;
            calls += 1;
            let version = host.query(room, &self.sql.version, &[])?[0][0].integer();
            self.rooms.entry(room).or_default().reset(version);
            let posted = Posted {
                id: inserted[0][0].integer(),
                room_id: room,
                user_id,
                body: &message.body,
                created_at: inserted[0][1].integer(),
                version,
            };
            return Ok(Response {
                status: 201,
                content_type: "application/json".into(),
                body: serde_json::to_vec(&posted).unwrap(),
                version,
                cache_hit: false,
                sql_calls: calls,
            });
        }

        calls += 1;
        let version = host.query(room, &self.sql.version, &[])?[0][0].integer();
        let cache = self.rooms.entry(room).or_default();
        if cache.version != version {
            cache.reset(version);
        }
        let key = (user_id, json);
        let content_type = if json {
            "application/json"
        } else {
            "text/html; charset=utf-8"
        };
        if request.cache {
            if let Some(body) = cache.entries.get(&key) {
                return Ok(Response {
                    status: 200,
                    content_type: content_type.into(),
                    body: body.clone(),
                    version,
                    cache_hit: true,
                    sql_calls: calls,
                });
            }
        }
        calls += 1;
        let rows = host.query(room, &self.sql.messages, &[])?;
        let page = Page {
            room_id: room,
            version,
            unread,
            messages: rows
                .into_iter()
                .map(|r| {
                    let mut columns = r.into_iter();
                    Message {
                        id: columns.next().unwrap().integer(),
                        user_id: columns.next().unwrap().integer(),
                        user_name: columns.next().unwrap().into_text(),
                        body: columns.next().unwrap().into_text(),
                        created_at: columns.next().unwrap().integer(),
                    }
                })
                .collect(),
        };
        let body = if json {
            serde_json::to_vec(&page).unwrap()
        } else {
            html(&page, &user_name)
        };
        if request.cache && body.len() <= CACHE_BYTES_PER_ROOM {
            if cache.bytes + body.len() > CACHE_BYTES_PER_ROOM {
                cache.reset(version);
            }
            cache.bytes += body.len();
            cache.entries.insert(key, body.clone());
        }
        Ok(Response {
            status: 200,
            content_type: content_type.into(),
            body,
            version,
            cache_hit: false,
            sql_calls: calls,
        })
    }
}

#[derive(Serialize, Deserialize)]
pub enum Action {
    Response(Response),
    Forward { room: u32, request: Request },
    Upgrade { socket: u64 },
}

#[derive(Deserialize)]
struct Schema {
    schema: Vec<String>,
    seed: Vec<String>,
    trigger: String,
}

pub fn room_route(path: &str) -> Option<(u32, &str)> {
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() < 3 || parts.len() > 4 || parts[0] != "" || parts[1] != "rooms" {
        return None;
    }
    let digits = parts[2];
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let room = digits.parse::<u32>().ok()?;
    if !(1..=ROOMS).contains(&room) {
        return None;
    }
    let suffix = parts.get(3).copied().unwrap_or("");
    if parts.len() == 4 && !matches!(suffix, "messages" | "read" | "ws") {
        return None;
    }
    Some((room, suffix))
}

pub fn ingress(request: Request) -> Action {
    if request.method == "GET" && request.path == "/" {
        return Action::Response(Response {
            status: 200,
            content_type: "text/html; charset=utf-8".into(),
            body: include_bytes!("../../application/index.html").to_vec(),
            version: 0,
            cache_hit: false,
            sql_calls: 0,
        });
    }
    if request.method == "POST" && request.path == "/login" {
        #[derive(Deserialize)]
        struct Login {
            user: u32,
            password: String,
        }
        let valid = serde_json::from_slice::<Login>(&request.body)
            .ok()
            .filter(|login| {
                (1..=8).contains(&login.user)
                    && login.password == format!("password-{}", login.user)
            });
        return Action::Response(match valid {
            Some(login) => Response {
                status: 200,
                content_type: "application/json".into(),
                body: format!("{{\"token\":\"token-{}\"}}", login.user).into_bytes(),
                version: 0,
                cache_hit: false,
                sql_calls: 0,
            },
            None => Response::error(401, "unauthorized", 0),
        });
    }
    let Some((room, suffix)) = room_route(&request.path) else {
        return Action::Response(Response::error(404, "not found", 0));
    };
    let allowed = match suffix {
        "" => request.method == "GET",
        "messages" => matches!(request.method.as_str(), "GET" | "POST"),
        "read" => request.method == "POST",
        "ws" => request.method == "GET" && request.upgrade,
        _ => false,
    };
    if !allowed {
        return Action::Response(Response::error(404, "not found", 0));
    }
    Action::Forward { room, request }
}

impl App {
    pub fn release(&mut self, room: u32) {
        self.rooms.remove(&room);
        self.initialized.remove(&room);
    }

    fn initialize(&mut self, host: &mut impl Host, room: u32) -> Result<(), String> {
        if self.initialized.contains(&room) {
            return Ok(());
        }
        let schema: Schema = serde_json::from_str(include_str!("../schema.json")).unwrap();
        for sql in &schema.schema {
            host.query(room, sql, &[])?;
        }
        if host.query(room, "SELECT count(*) FROM room_meta", &[])?[0][0].integer() == 0 {
            for (index, sql) in schema.seed.iter().enumerate() {
                let params = if index >= 2 {
                    vec![Value::Integer(room as i64)]
                } else {
                    vec![]
                };
                host.query(room, sql, &params)?;
            }
        }
        host.query(room, &schema.trigger, &[])?;
        self.initialized.insert(room);
        Ok(())
    }

    pub fn handle_cell(
        &mut self,
        host: &mut impl Host,
        request: Request,
    ) -> Result<Action, String> {
        let Some((room, suffix)) = room_route(&request.path) else {
            return Ok(Action::Response(Response::error(404, "not found", 0)));
        };
        self.initialize(host, room)?;
        if suffix == "ws" || suffix == "read" {
            let Some(token) = request.authorization.strip_prefix("Bearer ") else {
                return Ok(Action::Response(Response::error(401, "unauthorized", 0)));
            };
            let users = host.query(room, &self.sql.auth, &[Value::Text(token.into())])?;
            let Some(user) = users.first() else {
                return Ok(Action::Response(Response::error(401, "unauthorized", 1)));
            };
            if host
                .query(room, &self.sql.member, &[Value::Integer(user[0].integer())])?
                .is_empty()
            {
                return Ok(Action::Response(Response::error(403, "forbidden", 2)));
            }
            if suffix == "ws" {
                return Ok(Action::Upgrade {
                    socket: host.accept_socket(token)?,
                });
            }
            host.query(
                room,
                "UPDATE members SET unread=0 WHERE user_id=?",
                &[Value::Integer(user[0].integer())],
            )?;
            host.query(
                room,
                "UPDATE room_meta SET version=version+1 WHERE id=1",
                &[],
            )?;
            let version = host.query(room, &self.sql.version, &[])?[0][0].integer();
            self.rooms.entry(room).or_default().reset(version);
            return Ok(Action::Response(Response {
                status: 200,
                content_type: "application/json".into(),
                body: b"{\"ok\":true}".to_vec(),
                version,
                cache_hit: false,
                sql_calls: 5,
            }));
        }
        let response = self.render_or_post(host, request)?;
        if response.status == 201 {
            host.broadcast(&response.body)?;
        }
        Ok(Action::Response(response))
    }
}

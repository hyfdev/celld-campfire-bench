use crate::engine_api::{self, HttpResponse, HttpResponseWebSocket, RequestBody, WsTarget};
use crate::http_streams::RequestBodyGuard;
use crate::js::{CellJob, CellStorage, DoCallReq};
use crate::storage::{self, Cells, SqlValue};
use crate::ws_registry::{self, WsBatch, WsDispatch, WsIn, WsMeta, WsOut};
use anyhow::{anyhow, Context, Result};
use futures_util::future::BoxFuture;
use std::collections::HashMap;
use std::ops::Range;
use std::sync::{Arc, Mutex};
use workers_chat_app::{Action, App, Host, Request, Rows, Value};

pub struct DirectRuntime {
    engine: Engine,
    cells: Box<Cells>,
    host: Arc<Mutex<HostState>>,
    names: HashMap<String, u32>,
    script: String,
    pub task: Option<BoxFuture<'static, ()>>,
}

enum Engine {
    Native(App),
    Wasm(DirectWasm),
    WasmV8(V8Wasm),
}

#[derive(Default)]
struct HostState {
    scope: String,
    room: u32,
    input: Vec<u8>,
    output: Option<Action>,
    memory: Option<v8::Global<v8::WasmMemoryObject>>,
    frames: WsBatch,
}

impl Host for HostState {
    fn query(&mut self, room: u32, sql: &str, params: &[Value]) -> Result<Rows, String> {
        if self.scope.is_empty() || room != self.room {
            return Err("room storage capability mismatch".into());
        }
        let params: Vec<_> = params
            .iter()
            .map(|v| match v {
                Value::Integer(n) => serde_json::Value::from(*n),
                Value::Text(s) => serde_json::Value::from(s.clone()),
            })
            .collect();
        let (cursor, _, first, _, _) = storage::sql_cursor_start(&self.scope, sql, &params)?;
        if cursor == 0 {
            return Ok(vec![]);
        }
        let row = |row: Vec<SqlValue>| -> Result<Vec<Value>, String> {
            row.into_iter()
                .map(|v| match v {
                    SqlValue::Integer(n) => Ok(Value::Integer(n)),
                    SqlValue::Text(s) => Ok(Value::Text(s)),
                    _ => Err("unsupported application SQL value".into()),
                })
                .collect()
        };
        let out = (|| {
            let mut out = Vec::new();
            if let Some(first) = first {
                out.push(row(first)?);
            }
            while let Some(next) = storage::sql_cursor_next(cursor)?.0 {
                out.push(row(next)?);
            }
            Ok(out)
        })();
        storage::sql_cursor_close(cursor);
        out
    }

    fn accept_socket(&mut self, token: &str) -> Result<u64, String> {
        if self.scope.is_empty() {
            return Err("socket requires a room".into());
        }
        let id = ws_registry::ws_next_id();
        ws_registry::ws_registry().lock().unwrap().metadata.insert(
            id,
            WsMeta {
                scope: self.scope.clone(),
                hibernatable: true,
                tags: vec![token.into()],
                attachment: None,
                pending: vec![],
                auto_response_at: None,
                facet: None,
            },
        );
        Ok(id)
    }

    fn broadcast(&mut self, data: &[u8]) -> Result<(), String> {
        let text = std::str::from_utf8(data).map_err(|e| e.to_string())?;
        let ids: Vec<_> = ws_registry::ws_registry()
            .lock()
            .unwrap()
            .metadata
            .iter()
            .filter(|(_, m)| m.scope == self.scope && m.hibernatable)
            .map(|(id, _)| *id)
            .collect();
        for id in ids {
            self.frames.capture(id, WsOut::Text(text.into()));
        }
        Ok(())
    }
}

fn response(action: Action, scope: &str) -> Result<HttpResponse> {
    match action {
        Action::Response(r) => Ok(HttpResponse {
            status: r.status,
            body: r.body,
            stream: None,
            websocket: None,
            headers: vec![
                ("content-type".into(), r.content_type),
                ("x-chat-version".into(), r.version.to_string()),
                ("x-chat-cache-hit".into(), (r.cache_hit as u8).to_string()),
                ("x-chat-sql-calls".into(), r.sql_calls.to_string()),
            ],
            write_position: None,
            observed_position: None,
        }),
        Action::Upgrade { socket } => Ok(HttpResponse {
            status: 101,
            body: vec![],
            stream: None,
            headers: vec![],
            write_position: None,
            observed_position: None,
            websocket: Some(HttpResponseWebSocket::Cell(WsTarget {
                id: socket,
                scope: scope.into(),
                tunnel: None,
            })),
        }),
        Action::Forward { .. } => Err(anyhow!("a cell cannot forward its response")),
    }
}

fn request(
    url: &str,
    method: String,
    body: RequestBody,
    headers: &[(String, String)],
) -> Result<Request> {
    let url = url::Url::parse(url)?;
    let get = |name: &str| {
        headers
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
            .unwrap_or("")
    };
    let authorization = if get("authorization").is_empty() {
        url.query_pairs()
            .find(|(k, _)| k == "token")
            .map(|(_, v)| format!("Bearer {v}"))
            .unwrap_or_default()
    } else {
        get("authorization").to_owned()
    };
    let body = if method == "POST" && (url.path() == "/login" || url.path().ends_with("/messages"))
    {
        body.into_held_bytes()
            .context("the chat prototype requires buffered JSON request bodies")?
    } else {
        Vec::new()
    };
    Ok(Request {
        method,
        path: url.path().into(),
        body,
        authorization,
        upgrade: get("upgrade").eq_ignore_ascii_case("websocket"),
        cache: get("x-chat-cache") == "1",
    })
}

impl DirectRuntime {
    pub fn selected(script: &str) -> Result<Option<Self>> {
        let mode = std::env::var("WORKERS_CHAT_ENGINE").unwrap_or_else(|_| "js".into());
        if mode == "js" || mode == "wasm-js" {
            return Ok(None);
        }
        anyhow::ensure!(
            script == "workers-chat",
            "direct chat engine only accepts the workers-chat application"
        );
        let host = Arc::new(Mutex::new(HostState::default()));
        let engine = match mode.as_str() {
            "native" => Engine::Native(App::new()),
            "wasm" => {
                let path =
                    std::env::var("WORKERS_CHAT_WASM").context("WORKERS_CHAT_WASM is required")?;
                Engine::Wasm(DirectWasm::new(&std::fs::read(path)?, host.clone())?)
            }
            "wasm-v8" => {
                let path =
                    std::env::var("WORKERS_CHAT_WASM").context("WORKERS_CHAT_WASM is required")?;
                Engine::WasmV8(V8Wasm::new(&std::fs::read(path)?, host.clone())?)
            }
            _ => return Err(anyhow!("unknown WORKERS_CHAT_ENGINE")),
        };
        Ok(Some(Self {
            engine,
            cells: Box::default(),
            host,
            names: HashMap::new(),
            script: script.into(),
            task: None,
        }))
    }

    pub fn heap_bytes(&self) -> crate::js::HeapBytes {
        match &self.engine {
            Engine::WasmV8(w) => w.heap_bytes(),
            Engine::Wasm(w) => crate::js::HeapBytes {
                physical: 0,
                external: w.memory.data_size(&w.store) as u64,
            },
            Engine::Native(_) => crate::js::HeapBytes {
                physical: 0,
                external: 0,
            },
        }
    }

    fn call(&mut self, kind: i32, scope: &str, request: Request) -> Result<Action> {
        let _installed = self.cells.install();
        {
            let mut host = self.host.lock().unwrap();
            host.scope = scope.into();
            host.room = if scope.is_empty() {
                0
            } else {
                *self
                    .names
                    .get(scope)
                    .context("room has no object identity")?
            };
            if !scope.is_empty() {
                let (room, _) =
                    workers_chat_app::room_route(&request.path).context("invalid cell path")?;
                anyhow::ensure!(room == host.room, "request names another object's room");
            }
            host.output = None;
            host.frames = WsBatch::default();
        }
        match &mut self.engine {
            Engine::Native(app) => {
                if kind == 0 {
                    Ok(workers_chat_app::ingress(request))
                } else {
                    app.handle_cell(&mut *self.host.lock().unwrap(), request)
                        .map_err(anyhow::Error::msg)
                }
            }
            Engine::Wasm(w) => {
                self.host.lock().unwrap().input = postcard::to_allocvec(&request)?;
                w.call(kind)?;
                self.host
                    .lock()
                    .unwrap()
                    .output
                    .take()
                    .context("Wasm returned no action")
            }
            Engine::WasmV8(w) => {
                self.host.lock().unwrap().input = postcard::to_allocvec(&request)?;
                w.call(kind)?;
                self.host
                    .lock()
                    .unwrap()
                    .output
                    .take()
                    .context("Wasm returned no action")
            }
        }
    }

    pub fn own_cell(
        &mut self,
        scope: &str,
        opening: Option<CellStorage<'_>>,
    ) -> Result<celld_logic::wake::AlarmSnapshot> {
        let _installed = self.cells.install();
        if let Some(opening) = opening {
            storage::open_at_epoch(
                scope,
                opening.path,
                opening.epoch,
                opening.replicated_wake,
                opening.vfs,
                false,
            )?;
            if let Some(name) = storage::get_actor_name(scope)? {
                self.names.insert(
                    scope.into(),
                    name.parse().context("invalid persisted room name")?,
                );
            }
            storage::alarm_snapshot(scope)
        } else {
            if let Some(room) = self.names.remove(scope) {
                match &mut self.engine {
                    Engine::Native(app) => app.release(room),
                    Engine::Wasm(w) => w.release(room)?,
                    Engine::WasmV8(w) => w.release(room)?,
                }
            }
            storage::close(scope);
            Ok(celld_logic::wake::AlarmSnapshot::without_wake(None))
        }
    }

    pub fn set_id_name(&mut self, scope: &str, name: &str) -> Result<()> {
        let room: u32 = name.parse().context("invalid room name")?;
        if let Some(existing) = self.names.get(scope) {
            anyhow::ensure!(
                *existing == room,
                "room name conflicts with active identity"
            );
            return Ok(());
        }
        let namespace = engine_api::namespace_key(&self.script, "ChatRoom");
        let expected = format!(
            "ChatRoom:{}",
            engine_api::durable_object_id_hex(&engine_api::durable_object_id_for_name(
                &namespace, name
            ))
        );
        anyhow::ensure!(
            scope == expected,
            "room name does not match its object identity"
        );
        let _installed = self.cells.install();
        storage::set_actor_name(scope, name)?;
        self.names.insert(scope.into(), room);
        Ok(())
    }

    pub fn worker(&mut self, job: crate::WorkerJob) {
        match job {
            crate::WorkerJob::Fetch {
                url,
                method,
                body,
                headers,
                reply,
                ..
            } => {
                let action =
                    request(&url, method, body, &headers).and_then(|r| self.call(0, "", r));
                let script = self.script.clone();
                self.task = Some(Box::pin(async move {
                    let result = async {
                        match action? {
                            Action::Forward { room, request } => {
                                let name = room.to_string();
                                let namespace = engine_api::namespace_key(&script, "ChatRoom");
                                let id = engine_api::durable_object_id_for_name(&namespace, &name);
                                let scope =
                                    format!("ChatRoom:{}", engine_api::durable_object_id_hex(&id));
                                let (reply, rx) = tokio::sync::oneshot::channel();
                                let body = RequestBody::Bytes(request.body.into());
                                let call = DoCallReq {
                                    request_id: Some(engine_api::next_do_request_id()),
                                    cancel: None,
                                    deliver_abort_to_handler: false,
                                    scope,
                                    name: Some(name),
                                    url,
                                    method: request.method,
                                    body_guard: RequestBodyGuard::of(&body),
                                    body,
                                    headers,
                                    reply,
                                    order: None,
                                    parent: None,
                                };
                                anyhow::ensure!(
                                    crate::js::submit_do_call(call),
                                    "DO router is unavailable"
                                );
                                rx.await.context("DO router dropped response")?
                            }
                            other => response(other, ""),
                        }
                    }
                    .await;
                    let _ = reply.send(result);
                }));
            }
            crate::WorkerJob::Rpc { reply, .. } => {
                let _ = reply.send(Err(anyhow!("chat worker exposes fetch only")));
            }
            crate::WorkerJob::Queue { reply, .. } => {
                let _ = reply.send(Err(anyhow!("chat worker exposes fetch only")));
            }
        }
    }

    fn prepare(&mut self, scope: &str, name: Option<&str>) -> Result<Option<u64>> {
        if let Some(name) = name {
            self.set_id_name(scope, name)?;
        }
        let _installed = self.cells.install();
        Ok(storage::event_start_position(scope))
    }

    fn finish(
        &mut self,
        scope: &str,
        before: Option<u64>,
    ) -> Result<(Option<u64>, Option<u64>, WsBatch, Option<u64>)> {
        let _installed = self.cells.install();
        let sample = storage::write_position(scope)?;
        let write = sample.filter(|p| Some(*p) > before);
        let observed = storage::observed_position(scope, sample);
        let frames = std::mem::take(&mut self.host.lock().unwrap().frames);
        Ok((write, observed, frames, storage::activation_epoch(scope)))
    }

    pub fn cell(&mut self, job: CellJob) {
        match job {
            CellJob::Fetch {
                scope,
                name,
                url,
                method,
                body,
                headers,
                reply,
                ..
            } => {
                let result = (|| {
                    let before = self.prepare(&scope, name.as_deref())?;
                    let request = request(&url, method, body, &headers)?;
                    let action = self.call(1, &scope, request)?;
                    let mut response = response(action, &scope)?;
                    let (write, observed, frames, epoch) = self.finish(&scope, before)?;
                    response.write_position = write;
                    response.observed_position = observed;
                    if !frames.is_empty() {
                        let scope = scope.clone();
                        crate::asyncrt::op_handle().spawn(async move {
                            let ticket = crate::actor::GateTicket {
                                channel: celld_logic::Channel::WsHibernatable,
                                position: write,
                                observed,
                                epoch,
                            };
                            let verdict = crate::host_channels::request_gate(
                                crate::host_channels::GATE_TX.get(),
                                scope,
                                ticket,
                            )
                            .await
                            .map_err(|e| e.to_string());
                            frames.release(verdict);
                        });
                    }
                    Ok(response)
                })();
                let _ = reply.send(result);
            }
            CellJob::WsMessage {
                scope,
                ws_id,
                data,
                reply,
                ..
            } => {
                let result = (|| {
                    let before = self.prepare(&scope, None)?;
                    let token = ws_registry::ws_registry()
                        .lock()
                        .unwrap()
                        .metadata
                        .get(&ws_id)
                        .and_then(|m| m.tags.first())
                        .cloned()
                        .context("socket has no room session")?;
                    let room = *self
                        .names
                        .get(&scope)
                        .context("socket room is not resident")?;
                    let body = match data {
                        WsIn::Text(s) => s.into_bytes(),
                        WsIn::Binary(b) => b,
                    };
                    let action = self.call(
                        1,
                        &scope,
                        Request {
                            method: "POST".into(),
                            path: format!("/rooms/{room}/messages"),
                            authorization: format!("Bearer {token}"),
                            body,
                            ..Default::default()
                        },
                    )?;
                    if let Action::Response(r) = action {
                        if r.status != 201 {
                            self.host
                                .lock()
                                .unwrap()
                                .frames
                                .capture(ws_id, WsOut::Text(String::from_utf8(r.body)?));
                        }
                    }
                    let (write_position, observed_position, frames, _) =
                        self.finish(&scope, before)?;
                    Ok(WsDispatch {
                        frames,
                        write_position,
                        observed_position,
                    })
                })();
                let _ = reply.send(result);
            }
            CellJob::WsOpen { reply, .. } => {
                let _ = reply.send(Ok(None));
            }
            CellJob::WsClosed { ws_id, reply, .. } => {
                ws_registry::ws_close(ws_id, 1000, "");
                let _ = reply.send(Ok(None));
            }
            other => other.fail(anyhow!(
                "chat direct engine supports fetch and WebSocket events"
            )),
        }
    }
}

struct DirectWasmState {
    host: Arc<Mutex<HostState>>,
    limits: wasmtime::StoreLimits,
}

struct DirectWasm {
    store: wasmtime::Store<DirectWasmState>,
    memory: wasmtime::Memory,
    handle: wasmtime::TypedFunc<i32, ()>,
    release: wasmtime::TypedFunc<i32, ()>,
}

fn direct_memory<T>(
    caller: &mut wasmtime::Caller<'_, DirectWasmState>,
    f: impl FnOnce(&mut [u8], &mut HostState) -> Result<T>,
) -> wasmtime::Result<T> {
    let memory = caller
        .get_export("memory")
        .and_then(|e| e.into_memory())
        .ok_or_else(|| wasmtime::Error::msg("missing Wasm memory"))?;
    let (bytes, state) = memory.data_and_store_mut(caller);
    f(bytes, &mut state.host.lock().unwrap()).map_err(wasmtime::Error::from_anyhow)
}

impl DirectWasm {
    fn new(bytes: &[u8], host: Arc<Mutex<HostState>>) -> Result<Self> {
        static COMPILED: Mutex<Option<(wasmtime::Engine, wasmtime::Module)>> = Mutex::new(None);
        let (engine, module) = {
            let mut compiled = COMPILED.lock().unwrap();
            if compiled.is_none() {
                let mut config = wasmtime::Config::new();
                config.cranelift_opt_level(wasmtime::OptLevel::Speed);
                let cache = if let Some(path) = std::env::var_os("WORKERS_CHAT_COMPILE_CACHE") {
                    let mut cache_config = wasmtime::CacheConfig::new();
                    cache_config.with_directory(std::path::PathBuf::from(path));
                    Some(wasmtime::Cache::new(cache_config)?)
                } else {
                    None
                };
                config.cache(cache.clone());
                let engine = wasmtime::Engine::new(&config)?;
                let started = std::time::Instant::now();
                let module = wasmtime::Module::new(&engine, bytes)?;
                let module_ms = started.elapsed().as_secs_f64() * 1000.0;
                eprintln!(
                    "CHAT_WASMTIME_MODULE {}",
                    serde_json::json!({
                        "module_ms": module_ms,
                        "cache_enabled": cache.is_some(),
                        "cache_hits": cache.as_ref().map_or(0, |c| c.cache_hits()),
                        "cache_misses": cache.as_ref().map_or(0, |c| c.cache_misses()),
                    })
                );
                *compiled = Some((engine, module));
            }
            compiled.as_ref().unwrap().clone()
        };
        let mut linker = wasmtime::Linker::new(&engine);
        linker.func_wrap(
            "chat",
            "input",
            |mut caller: wasmtime::Caller<'_, DirectWasmState>, ptr: i32, cap: i32| {
                direct_memory(&mut caller, |bytes, host| {
                    if cap != 0 {
                        let range = bounds(ptr, cap, bytes.len())?;
                        anyhow::ensure!(
                            range.len() >= host.input.len(),
                            "input buffer is too small"
                        );
                        bytes[range.start..range.start + host.input.len()]
                            .copy_from_slice(&host.input);
                    }
                    Ok(host.input.len() as i32)
                })
            },
        )?;
        linker.func_wrap(
            "chat",
            "sql_rows",
            |mut caller: wasmtime::Caller<'_, DirectWasmState>,
             room: i32,
             sql: i32,
             sql_len: i32,
             params: i32,
             params_len: i32,
             out: i32,
             cap: i32| {
                direct_memory(&mut caller, |bytes, host| {
                    let sql =
                        std::str::from_utf8(&bytes[bounds(sql, sql_len, bytes.len())?])?.to_owned();
                    let params: Vec<Value> =
                        postcard::from_bytes(&bytes[bounds(params, params_len, bytes.len())?])?;
                    let rows = host
                        .query(room as u32, &sql, &params)
                        .map_err(anyhow::Error::msg)?;
                    let range = bounds(out, cap, bytes.len())?;
                    Ok(postcard::to_slice(&rows, &mut bytes[range])?.len() as i32)
                })
            },
        )?;
        linker.func_wrap(
            "chat",
            "output",
            |mut caller: wasmtime::Caller<'_, DirectWasmState>, ptr: i32, len: i32| {
                direct_memory(&mut caller, |bytes, host| {
                    anyhow::ensure!(host.output.is_none(), "duplicate Wasm response");
                    host.output = Some(postcard::from_bytes(
                        &bytes[bounds(ptr, len, bytes.len())?],
                    )?);
                    Ok(())
                })
            },
        )?;
        linker.func_wrap(
            "chat",
            "accept_socket",
            |mut caller: wasmtime::Caller<'_, DirectWasmState>, ptr: i32, len: i32| {
                direct_memory(&mut caller, |bytes, host| {
                    let token = std::str::from_utf8(&bytes[bounds(ptr, len, bytes.len())?])?;
                    let id = host.accept_socket(token).map_err(anyhow::Error::msg)?;
                    Ok(i32::try_from(id)?)
                })
            },
        )?;
        linker.func_wrap(
            "chat",
            "broadcast",
            |mut caller: wasmtime::Caller<'_, DirectWasmState>, ptr: i32, len: i32| {
                direct_memory(&mut caller, |bytes, host| {
                    host.broadcast(&bytes[bounds(ptr, len, bytes.len())?])
                        .map_err(anyhow::Error::msg)
                })
            },
        )?;
        let mut store = wasmtime::Store::new(
            &engine,
            DirectWasmState {
                host,
                limits: wasmtime::StoreLimitsBuilder::new()
                    .memory_size(128 * 1024 * 1024)
                    .instances(1)
                    .memories(1)
                    .tables(1)
                    .build(),
            },
        );
        store.limiter(|state| &mut state.limits);
        let instance = linker.instantiate(&mut store, &module)?;
        instance
            .get_typed_func::<(), ()>(&mut store, "init")?
            .call(&mut store, ())?;
        let memory = instance
            .get_memory(&mut store, "memory")
            .context("missing Wasm memory")?;
        let handle = instance.get_typed_func(&mut store, "handle")?;
        let release = instance.get_typed_func(&mut store, "release")?;
        Ok(Self {
            store,
            memory,
            handle,
            release,
        })
    }

    fn call(&mut self, kind: i32) -> Result<()> {
        self.handle.call(&mut self.store, kind)?;
        Ok(())
    }

    fn release(&mut self, room: u32) -> Result<()> {
        self.release.call(&mut self.store, room as i32)?;
        Ok(())
    }
}

struct V8Wasm {
    isolate: v8::SharedIsolate,
    context: v8::Global<v8::Context>,
    handle: v8::Global<v8::Function>,
    release: v8::Global<v8::Function>,
}

fn get<'s>(
    scope: &mut v8::PinScope<'s, '_>,
    object: v8::Local<v8::Object>,
    name: &str,
) -> Result<v8::Local<'s, v8::Value>> {
    let key = v8::String::new(scope, name).context("V8 string allocation")?;
    object.get(scope, key.into()).context("missing V8 property")
}

fn bounds(ptr: i32, len: i32, total: usize) -> Result<Range<usize>> {
    anyhow::ensure!(len >= 0, "negative Wasm buffer length");
    let start = ptr as u32 as usize;
    let end = start
        .checked_add(len as usize)
        .context("Wasm buffer overflow")?;
    anyhow::ensure!(end <= total, "Wasm buffer is outside linear memory");
    Ok(start..end)
}

fn with_memory<T>(
    scope: &mut v8::PinScope,
    f: impl FnOnce(&mut [u8], &mut HostState) -> Result<T>,
) -> Result<T> {
    let state = scope
        .get_slot::<Arc<Mutex<HostState>>>()
        .context("missing Wasm host state")?
        .clone();
    let mut state = state.lock().unwrap();
    let memory = v8::Local::new(scope, state.memory.as_ref().context("missing Wasm memory")?);
    let backing = memory.buffer().get_backing_store();
    let ptr = backing.data().context("missing Wasm backing store")?;
    // The celld pool holds the isolate's turn permit; Wasm is suspended in this callback.
    let bytes =
        unsafe { std::slice::from_raw_parts_mut(ptr.as_ptr() as *mut u8, backing.byte_length()) };
    f(bytes, &mut state)
}

fn failure(scope: &mut v8::PinScope, error: anyhow::Error) {
    let message = format!("{error:#}");
    let s = v8::String::new(scope, &message).unwrap();
    let e = v8::Exception::error(scope, s);
    scope.throw_exception(e);
}

fn input(
    scope: &mut v8::PinScope,
    args: v8::FunctionCallbackArguments,
    mut rv: v8::ReturnValue<v8::Value>,
) {
    let ptr = args.get(0).int32_value(scope).unwrap_or(-1);
    let cap = args.get(1).int32_value(scope).unwrap_or(-1);
    let out = with_memory(scope, |mem, host| {
        if cap != 0 {
            let range = bounds(ptr, cap, mem.len())?;
            anyhow::ensure!(range.len() >= host.input.len(), "input buffer is too small");
            mem[range.start..range.start + host.input.len()].copy_from_slice(&host.input);
        }
        Ok(host.input.len() as i32)
    });
    match out {
        Ok(n) => rv.set_int32(n),
        Err(e) => failure(scope, e),
    }
}

fn sql_rows(
    scope: &mut v8::PinScope,
    args: v8::FunctionCallbackArguments,
    mut rv: v8::ReturnValue<v8::Value>,
) {
    let mut a = [0i32; 7];
    for (i, n) in a.iter_mut().enumerate() {
        *n = args.get(i as i32).int32_value(scope).unwrap_or(-1);
    }
    let out = with_memory(scope, |mem, host| {
        let sql = std::str::from_utf8(&mem[bounds(a[1], a[2], mem.len())?])?.to_owned();
        let params: Vec<Value> = postcard::from_bytes(&mem[bounds(a[3], a[4], mem.len())?])?;
        let rows = host
            .query(a[0] as u32, &sql, &params)
            .map_err(anyhow::Error::msg)?;
        let range = bounds(a[5], a[6], mem.len())?;
        Ok(postcard::to_slice(&rows, &mut mem[range])?.len() as i32)
    });
    match out {
        Ok(n) => rv.set_int32(n),
        Err(e) => failure(scope, e),
    }
}

fn output(
    scope: &mut v8::PinScope,
    args: v8::FunctionCallbackArguments,
    _rv: v8::ReturnValue<v8::Value>,
) {
    let ptr = args.get(0).int32_value(scope).unwrap_or(-1);
    let len = args.get(1).int32_value(scope).unwrap_or(-1);
    let out = with_memory(scope, |mem, host| {
        anyhow::ensure!(host.output.is_none(), "duplicate Wasm response");
        host.output = Some(postcard::from_bytes(&mem[bounds(ptr, len, mem.len())?])?);
        Ok(())
    });
    if let Err(e) = out {
        failure(scope, e);
    }
}

fn accept_socket(
    scope: &mut v8::PinScope,
    args: v8::FunctionCallbackArguments,
    mut rv: v8::ReturnValue<v8::Value>,
) {
    let ptr = args.get(0).int32_value(scope).unwrap_or(-1);
    let len = args.get(1).int32_value(scope).unwrap_or(-1);
    let out = with_memory(scope, |mem, host| {
        host.accept_socket(std::str::from_utf8(&mem[bounds(ptr, len, mem.len())?])?)
            .map_err(anyhow::Error::msg)
    });
    match out {
        Ok(n) => rv.set_int32(n.try_into().unwrap()),
        Err(e) => failure(scope, e),
    }
}

fn broadcast(
    scope: &mut v8::PinScope,
    args: v8::FunctionCallbackArguments,
    _rv: v8::ReturnValue<v8::Value>,
) {
    let ptr = args.get(0).int32_value(scope).unwrap_or(-1);
    let len = args.get(1).int32_value(scope).unwrap_or(-1);
    let out = with_memory(scope, |mem, host| {
        host.broadcast(&mem[bounds(ptr, len, mem.len())?])
            .map_err(anyhow::Error::msg)
    });
    if let Err(e) = out {
        failure(scope, e);
    }
}

impl V8Wasm {
    fn new(bytes: &[u8], host: Arc<Mutex<HostState>>) -> Result<Self> {
        let mut isolate =
            v8::Isolate::new(v8::CreateParams::default().heap_limits(0, 128 * 1024 * 1024));
        isolate.set_slot(host.clone());
        let (context, handle, release) = {
            v8::scope!(let scope,&mut isolate);
            let context = v8::Context::new(scope, Default::default());
            let scope = &mut v8::ContextScope::new(scope, context);
            let tc = std::pin::pin!(v8::TryCatch::new(scope));
            let scope = &mut tc.init();
            let module =
                v8::WasmModuleObject::compile(scope, bytes).context("compile Rust Wasm in V8")?;
            let imports = v8::Object::new(scope);
            let chat = v8::Object::new(scope);
            macro_rules! binding {
                ($name:literal,$callback:ident) => {{
                    let name = v8::String::new(scope, $name).unwrap();
                    let func = v8::Function::new(scope, $callback).unwrap();
                    chat.set(scope, name.into(), func.into());
                }};
            }
            binding!("input", input);
            binding!("output", output);
            binding!("sql_rows", sql_rows);
            binding!("accept_socket", accept_socket);
            binding!("broadcast", broadcast);
            let key = v8::String::new(scope, "chat").unwrap();
            imports.set(scope, key.into(), chat.into());
            let global = context.global(scope);
            let webassembly = get(scope, global, "WebAssembly")?
                .to_object(scope)
                .context("WebAssembly built-in")?;
            let constructor = get(scope, webassembly, "Instance")?
                .try_cast::<v8::Function>()
                .map_err(|_| anyhow!("WebAssembly.Instance built-in"))?;
            let instance = constructor
                .new_instance(scope, &[module.into(), imports.into()])
                .context("instantiate Rust Wasm with Rust imports")?;
            let exports = get(scope, instance, "exports")?
                .to_object(scope)
                .context("Wasm exports")?;
            let memory = get(scope, exports, "memory")?
                .try_cast::<v8::WasmMemoryObject>()
                .map_err(|_| anyhow!("Wasm memory export"))?;
            host.lock().unwrap().memory = Some(v8::Global::new(scope, memory));
            let init = get(scope, exports, "init")?
                .try_cast::<v8::Function>()
                .map_err(|_| anyhow!("Wasm init export"))?;
            let undefined = v8::undefined(scope).into();
            init.call(scope, undefined, &[])
                .context("initialize Rust Wasm application")?;
            let handle = get(scope, exports, "handle")?
                .try_cast::<v8::Function>()
                .map_err(|_| anyhow!("Wasm handler export"))?;
            let release = get(scope, exports, "release")?
                .try_cast::<v8::Function>()
                .map_err(|_| anyhow!("Wasm release export"))?;
            (
                v8::Global::new(scope, context),
                v8::Global::new(scope, handle),
                v8::Global::new(scope, release),
            )
        };
        isolate.low_memory_notification();
        Ok(Self {
            isolate: unsafe { isolate.try_into_shared() }
                .map_err(|e| anyhow!("share Wasm isolate: {e}"))?,
            context,
            handle,
            release,
        })
    }

    fn invoke(&mut self, release: bool, value: i32) -> Result<()> {
        let mut locker = self.isolate.lock();
        v8::scope!(let scope,&mut *locker);
        let context = v8::Local::new(scope, &self.context);
        let scope = &mut v8::ContextScope::new(scope, context);
        let tc = std::pin::pin!(v8::TryCatch::new(scope));
        let scope = &mut tc.init();
        let f = v8::Local::new(scope, if release { &self.release } else { &self.handle });
        let argument = v8::Integer::new(scope, value).into();
        let undefined = v8::undefined(scope).into();
        if f.call(scope, undefined, &[argument]).is_none() {
            let message = scope
                .exception()
                .map(|e| e.to_rust_string_lossy(scope))
                .unwrap_or_else(|| "Wasm execution terminated".into());
            return Err(anyhow!(message));
        }
        Ok(())
    }
    fn call(&mut self, kind: i32) -> Result<()> {
        self.invoke(false, kind)
    }
    fn release(&mut self, room: u32) -> Result<()> {
        self.invoke(true, room as i32)
    }
    fn heap_bytes(&self) -> crate::js::HeapBytes {
        let mut locker = self.isolate.lock();
        let s = locker.get_heap_statistics();
        crate::js::HeapBytes {
            physical: s.total_physical_size() as u64,
            external: s.external_memory() as u64,
        }
    }
}

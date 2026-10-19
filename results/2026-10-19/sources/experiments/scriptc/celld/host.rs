use super::*;
use std::sync::OnceLock;

fn host_call(host: &mut HostState, operation: &str, payload: &str) -> Result<Vec<u8>> {
    match operation {
        "sql" => {
            #[derive(serde::Deserialize)]
            struct Query {
                room: u32,
                sql: String,
                params: Vec<serde_json::Value>,
            }
            let q: Query = serde_json::from_str(payload)?;
            let params = q
                .params
                .into_iter()
                .map(|v| match v {
                    serde_json::Value::String(s) => Ok(Value::Text(s)),
                    serde_json::Value::Number(n) => {
                        Ok(Value::Integer(n.as_i64().context("SQL integer")?))
                    }
                    _ => Err(anyhow!("unsupported SQL parameter")),
                })
                .collect::<Result<Vec<_>>>()?;
            let rows = host
                .query(q.room, &q.sql, &params)
                .map_err(anyhow::Error::msg)?;
            let rows: Vec<Vec<serde_json::Value>> = rows
                .into_iter()
                .map(|r| {
                    r.into_iter()
                        .map(|v| match v {
                            Value::Integer(n) => n.into(),
                            Value::Text(s) => s.into(),
                        })
                        .collect()
                })
                .collect();
            Ok(serde_json::to_vec(&rows)?)
        }
        "accept" => Ok(host
            .accept_socket(payload)
            .map_err(anyhow::Error::msg)?
            .to_string()
            .into_bytes()),
        "broadcast" => {
            host.broadcast(payload.as_bytes())
                .map_err(anyhow::Error::msg)?;
            Ok(Vec::new())
        }
        _ => Err(anyhow!("unknown TS host call")),
    }
}

fn encode_request(request: &Request) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec(&serde_json::json!({
        "method": request.method, "path": request.path, "authorization": request.authorization,
        "body": String::from_utf8_lossy(&request.body), "upgrade": request.upgrade, "cache": request.cache,
    }))?)
}

fn decode_action(bytes: &[u8], request: Request) -> Result<Action> {
    #[derive(serde::Deserialize)]
    struct Output {
        kind: String,
        room: u32,
        socket: u64,
        status: u16,
        content_type: String,
        body: String,
        version: i64,
        cache_hit: bool,
        sql_calls: u32,
    }
    let a: Output = serde_json::from_slice(bytes)?;
    Ok(match a.kind.as_str() {
        "forward" => Action::Forward {
            room: a.room,
            request,
        },
        "upgrade" => Action::Upgrade { socket: a.socket },
        "response" => Action::Response(workers_chat_app::Response {
            status: a.status,
            content_type: a.content_type,
            body: a.body.into_bytes(),
            version: a.version,
            cache_hit: a.cache_hit,
            sql_calls: a.sql_calls,
        }),
        _ => return Err(anyhow!("invalid TS action")),
    })
}

impl TsRuntime {
    pub fn call(&mut self, kind: i32, request: Request) -> Result<Action> {
        let input = encode_request(&request)?;
        let output = self.call_bytes(kind, &input)?;
        decode_action(&output, request)
    }
    pub fn release(&mut self, room: u32) -> Result<()> {
        self.release.call(&mut self.store, room as f64)?;
        Ok(())
    }
    pub fn heap_bytes(&self) -> crate::js::HeapBytes {
        crate::js::HeapBytes {
            physical: 0,
            external: self.memory.data_size(&self.store) as u64,
        }
    }
}

struct WasmState {
    host: Arc<Mutex<HostState>>,
    limits: wasmtime::StoreLimits,
}
pub struct TsRuntime {
    store: wasmtime::Store<WasmState>,
    memory: wasmtime::Memory,
    handle: wasmtime::TypedFunc<(f64, i32, i32, i32, i32), ()>,
    release: wasmtime::TypedFunc<f64, ()>,
    alloc: wasmtime::TypedFunc<i32, i32>,
    free: wasmtime::TypedFunc<i32, ()>,
    input: i32,
    capacity: usize,
    slots: i32,
}
fn memory(c: &mut wasmtime::Caller<'_, WasmState>) -> wasmtime::Result<wasmtime::Memory> {
    c.get_export("memory")
        .and_then(|e| e.into_memory())
        .ok_or_else(|| wasmtime::Error::msg("TS memory missing"))
}
fn write_i32(c: &mut wasmtime::Caller<'_, WasmState>, p: i32, v: i32) -> wasmtime::Result<()> {
    memory(c)?
        .write(&mut *c, p as u32 as usize, &v.to_le_bytes())
        .map_err(Into::into)
}

impl TsRuntime {
    pub fn new(host: Arc<Mutex<HostState>>) -> Result<Self> {
        static MODULE: OnceLock<(wasmtime::Engine, wasmtime::Module)> = OnceLock::new();
        if MODULE.get().is_none() {
            let engine = wasmtime::Engine::default();
            let bytes = std::fs::read(std::env::var("WORKERS_CHAT_TS_WASM")?)?;
            let module = wasmtime::Module::new(&engine, bytes)?;
            let _ = MODULE.set((engine, module));
        }
        let (engine, module) = MODULE.get().unwrap();
        let mut linker = wasmtime::Linker::new(engine);
        linker.func_wrap(
            "scriptc",
            "hostCall",
            |mut c: wasmtime::Caller<'_, WasmState>,
             op: i32,
             opn: i32,
             input: i32,
             inputn: i32,
             out: i32,
             cap: i32|
             -> wasmtime::Result<f64> {
                let result = (|| -> Result<f64> {
                    let mem = memory(&mut c)?;
                    let bytes = mem.data(&c);
                    let operation =
                        std::str::from_utf8(&bytes[bounds(op, opn, bytes.len())?])?.to_owned();
                    let input = std::str::from_utf8(&bytes[bounds(input, inputn, bytes.len())?])?
                        .to_owned();
                    let output = host_call(&mut c.data().host.lock().unwrap(), &operation, &input)?;
                    anyhow::ensure!(output.len() <= cap as usize, "TS SQL output exceeds buffer");
                    let range = bounds(out, cap, mem.data_size(&c))?;
                    mem.write(&mut c, range.start, &output)?;
                    Ok(output.len() as f64)
                })();
                result.map_err(wasmtime::Error::from_anyhow)
            },
        )?;
        linker.func_wrap(
            "scriptc",
            "panic",
            |mut c: wasmtime::Caller<'_, WasmState>, p: i32, n: i32| -> wasmtime::Result<()> {
                let mem = memory(&mut c)?;
                Err(wasmtime::Error::msg(format!(
                    "scriptc panic: {}",
                    String::from_utf8_lossy(
                        &mem.data(&c)[p as u32 as usize..p as u32 as usize + n as u32 as usize]
                    )
                )))
            },
        )?;
        let wasi = "wasi_snapshot_preview1";
        linker.func_wrap(
            wasi,
            "environ_sizes_get",
            |mut c: wasmtime::Caller<'_, WasmState>, n: i32, size: i32| -> wasmtime::Result<i32> {
                write_i32(&mut c, n, 0)?;
                write_i32(&mut c, size, 0)?;
                Ok(0)
            },
        )?;
        linker.func_wrap(wasi, "environ_get", |_a: i32, _b: i32| -> i32 { 0 })?;
        linker.func_wrap(wasi, "fd_close", |_fd: i32| -> i32 { 8 })?;
        linker.func_wrap(
            wasi,
            "fd_seek",
            |_fd: i32, _offset: i64, _whence: i32, _result: i32| -> i32 { 8 },
        )?;
        linker.func_wrap(wasi, "proc_exit", |code: i32| -> wasmtime::Result<()> {
            Err(wasmtime::Error::msg(format!("WASI exit {code}")))
        })?;
        linker.func_wrap(
            wasi,
            "clock_time_get",
            |mut c: wasmtime::Caller<'_, WasmState>,
             clock: i32,
             _precision: i64,
             out: i32|
             -> wasmtime::Result<i32> {
                let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
                let id = match clock {
                    0 => libc::CLOCK_REALTIME,
                    1 => libc::CLOCK_MONOTONIC,
                    _ => return Ok(28),
                };
                if unsafe { libc::clock_gettime(id, &mut ts) } != 0 {
                    return Ok(29);
                }
                let n = ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64;
                memory(&mut c)?.write(&mut c, out as u32 as usize, &n.to_le_bytes())?;
                Ok(0)
            },
        )?;
        linker.func_wrap(
            wasi,
            "fd_fdstat_get",
            |mut c: wasmtime::Caller<'_, WasmState>, fd: i32, out: i32| -> wasmtime::Result<i32> {
                if !(0..=2).contains(&fd) {
                    return Ok(8);
                }
                let mut bytes = [0; 24];
                bytes[0] = 2;
                memory(&mut c)?.write(&mut c, out as u32 as usize, &bytes)?;
                Ok(0)
            },
        )?;
        linker.func_wrap(
            wasi,
            "fd_write",
            |mut c: wasmtime::Caller<'_, WasmState>,
             fd: i32,
             iovs: i32,
             count: i32,
             written: i32|
             -> wasmtime::Result<i32> {
                if fd != 1 && fd != 2 {
                    return Ok(8);
                }
                let mem = memory(&mut c)?;
                let bytes = mem.data(&c);
                let mut output = Vec::new();
                for i in 0..count {
                    let start = (iovs + i * 8) as u32 as usize;
                    let p =
                        u32::from_le_bytes(bytes[start..start + 4].try_into().unwrap()) as usize;
                    let n = u32::from_le_bytes(bytes[start + 4..start + 8].try_into().unwrap())
                        as usize;
                    output.extend_from_slice(&bytes[p..p + n]);
                }
                use std::io::Write;
                std::io::stderr().write_all(&output)?;
                write_i32(&mut c, written, output.len() as i32)?;
                Ok(0)
            },
        )?;
        let mut store = wasmtime::Store::new(
            engine,
            WasmState {
                host,
                limits: wasmtime::StoreLimitsBuilder::new()
                    .memory_size(128 * 1024 * 1024)
                    .instances(1)
                    .memories(1)
                    .tables(1)
                    .build(),
            },
        );
        store.limiter(|s| &mut s.limits);
        let instance = linker.instantiate(&mut store, module)?;
        instance
            .get_typed_func::<(), ()>(&mut store, "_initialize")?
            .call(&mut store, ())?;
        instance
            .get_typed_func::<(), ()>(&mut store, "chat_ts_init")?
            .call(&mut store, ())?;
        let memory = instance
            .get_memory(&mut store, "memory")
            .context("TS memory")?;
        let alloc = instance.get_typed_func::<i32, i32>(&mut store, "scriptc_alloc")?;
        let free = instance.get_typed_func(&mut store, "scriptc_free")?;
        let slots = alloc.call(&mut store, 8)?;
        let handle = instance.get_typed_func(&mut store, "chat_ts_handle")?;
        let release = instance.get_typed_func(&mut store, "chat_ts_release")?;
        Ok(Self {
            store,
            memory,
            handle,
            release,
            alloc,
            free,
            input: 0,
            capacity: 0,
            slots,
        })
    }
    fn call_bytes(&mut self, kind: i32, input: &[u8]) -> Result<Vec<u8>> {
        if input.len() > self.capacity {
            if self.input != 0 {
                self.free.call(&mut self.store, self.input)?;
            }
            self.capacity = input.len().next_power_of_two();
            self.input = self.alloc.call(&mut self.store, self.capacity as i32)?;
            anyhow::ensure!(self.input != 0, "TS input allocation failed");
        }
        self.memory
            .write(&mut self.store, self.input as u32 as usize, input)?;
        self.handle.call(
            &mut self.store,
            (
                kind as f64,
                self.input,
                input.len() as i32,
                self.slots,
                self.slots + 4,
            ),
        )?;
        let bytes = self.memory.data(&self.store);
        let slots = self.slots as u32 as usize;
        let p = u32::from_le_bytes(bytes[slots..slots + 4].try_into()?) as usize;
        let n = u32::from_le_bytes(bytes[slots + 4..slots + 8].try_into()?) as usize;
        Ok(bytes.get(p..p + n).context("TS result bounds")?.to_vec())
    }
}

use workers_chat_app::{Action, App, Host, Request, Rows, Value};

static mut STATE: Option<(App, Imports)> = None;

#[link(wasm_import_module = "chat")]
extern "C" {
    fn input(ptr: *mut u8, cap: i32) -> i32;
    fn sql_rows(
        room: i32,
        sql: *const u8,
        sql_len: i32,
        params: *const u8,
        params_len: i32,
        out: *mut u8,
        cap: i32,
    ) -> i32;
    fn output(ptr: *const u8, len: i32);
    fn accept_socket(ptr: *const u8, len: i32) -> i32;
    fn broadcast(ptr: *const u8, len: i32);
}

struct Imports {
    buffer: Vec<u8>,
}

impl Host for Imports {
    fn query(&mut self, room: u32, sql: &str, params: &[Value]) -> Result<Rows, String> {
        let params = postcard::to_allocvec(params).unwrap();
        let n = unsafe {
            sql_rows(
                room as i32,
                sql.as_ptr(),
                sql.len() as i32,
                params.as_ptr(),
                params.len() as i32,
                self.buffer.as_mut_ptr(),
                self.buffer.len() as i32,
            )
        };
        assert!(n >= 0 && n as usize <= self.buffer.len());
        postcard::from_bytes(&self.buffer[..n as usize]).map_err(|e| e.to_string())
    }
    fn accept_socket(&mut self, token: &str) -> Result<u64, String> {
        let id = unsafe { accept_socket(token.as_ptr(), token.len() as i32) };
        assert!(id > 0);
        Ok(id as u64)
    }
    fn broadcast(&mut self, data: &[u8]) -> Result<(), String> {
        unsafe { broadcast(data.as_ptr(), data.len() as i32) };
        Ok(())
    }
}

#[no_mangle]
pub extern "C" fn init() {
    unsafe {
        *std::ptr::addr_of_mut!(STATE) = Some((
            App::new(),
            Imports {
                buffer: vec![0; 512 * 1024],
            },
        ));
    }
}

#[no_mangle]
pub extern "C" fn release(room: i32) {
    let (app, _) = unsafe { (&mut *std::ptr::addr_of_mut!(STATE)).as_mut().unwrap() };
    app.release(room as u32);
}

#[no_mangle]
pub extern "C" fn handle(kind: i32) {
    let size = unsafe { input(std::ptr::null_mut(), 0) };
    assert!(size >= 0);
    let mut bytes = vec![0; size as usize];
    if size > 0 {
        assert_eq!(unsafe { input(bytes.as_mut_ptr(), size) }, size);
    }
    let request: Request = postcard::from_bytes(&bytes).unwrap();
    let action: Action = if kind == 0 {
        workers_chat_app::ingress(request)
    } else {
        let (app, imports) = unsafe { (&mut *std::ptr::addr_of_mut!(STATE)).as_mut().unwrap() };
        app.handle_cell(imports, request).unwrap()
    };
    let bytes = postcard::to_allocvec(&action).unwrap();
    unsafe { output(bytes.as_ptr(), bytes.len() as i32) };
}

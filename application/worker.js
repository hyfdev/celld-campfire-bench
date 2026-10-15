const SQL = {
  "auth": "SELECT id, name FROM users WHERE token = ?",
  "member": "SELECT unread FROM members WHERE user_id = ?",
  "version": "SELECT version FROM room_meta WHERE id = 1",
  "messages": "SELECT m.id, m.user_id, u.name, m.body, m.created_at FROM messages m JOIN users u ON u.id = m.user_id ORDER BY m.id DESC LIMIT 50",
  "insert": "INSERT INTO messages(user_id, body, created_at) VALUES (?, ?, 1700000000 + (SELECT version FROM room_meta WHERE id = 1)) RETURNING id, created_at"
}
;
const SCHEMA = {
  "schema": [
    "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE)",
    "CREATE TABLE IF NOT EXISTS members(user_id INTEGER PRIMARY KEY, unread INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS room_meta(id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
  ],
  "seed": [
    "INSERT INTO room_meta VALUES(1,1)",
    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<8) INSERT INTO users SELECT x, 'User '||x||' — 云飞 <u'||x||'> & \"'||char(39), 'token-'||x FROM n",
    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<7) INSERT INTO members SELECT x, ?1*2+x*3 FROM n",
    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000) INSERT INTO messages SELECT x,(x-1)%7+1,printf('Room %d, message %d: 你好，世界 👋\nLink: https://example.com/path?q=a&room=%d <script>alert(''quoted \"value\"'')</script> & text\n',?1,x,?1)||printf('Room %d, message %d: 你好，世界 👋\nLink: https://example.com/path?q=a&room=%d <script>alert(''quoted \"value\"'')</script> & text\n',?1,x,?1)||printf('Room %d, message %d: 你好，世界 👋\nLink: https://example.com/path?q=a&room=%d <script>alert(''quoted \"value\"'')</script> & text\n',?1,x,?1),1699900000+x FROM n"
  ],
  "trigger": "CREATE TRIGGER IF NOT EXISTS message_inserted AFTER INSERT ON messages BEGIN UPDATE room_meta SET version=version+1 WHERE id=1; UPDATE members SET unread=unread+1 WHERE user_id!=NEW.user_id; END"
}
;
const UI = "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>Workers Chat</title><style>\nbody{font:16px system-ui;max-width:960px;margin:2rem auto;padding:0 1rem}header,form{display:flex;gap:.6rem;align-items:center}select,input,button,textarea{font:inherit;padding:.6rem}textarea{flex:1}article{border-bottom:1px solid #ddd;padding:.5rem 0}article p{white-space:pre-wrap;overflow-wrap:anywhere}#messages{height:60vh;overflow:auto}#status{color:#666}h2{font-size:1rem}\n</style></head><body><h1>Workers Chat</h1><form id=\"login\"><label>User <input id=\"user\" type=\"number\" min=\"1\" max=\"7\" value=\"1\"></label><input id=\"password\" type=\"password\" value=\"password-1\"><button>Log in</button></form><p>Demo accounts: users 1\u20137, password-N.</p><header><label>Room <select id=\"room\"></select></label><button id=\"join\">Join</button><span id=\"status\">Disconnected</span></header><main id=\"messages\"></main><form id=\"send\"><textarea id=\"body\" required maxlength=\"8192\" placeholder=\"Write a message\"></textarea><button>Send</button></form><script>\nlet token=localStorage.getItem('chat-token')||'',socket;\nconst $=id=>document.getElementById(id),headers=()=>({'authorization':'Bearer '+token,'content-type':'application/json','x-chat-cache':'1'});\nfor(let i=1;i<=50;i++)$('room').add(new Option('Room '+i,i));\nfunction append(m){const a=document.createElement('article'),h=document.createElement('h2'),p=document.createElement('p');a.dataset.id=m.id;h.textContent=m.user_name||'User '+m.user_id;p.textContent=m.body;a.append(h,p);$('messages').append(a);$('messages').scrollTop=$('messages').scrollHeight;}\n$('login').onsubmit=async e=>{e.preventDefault();const r=await fetch('/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({user:Number($('user').value),password:$('password').value})});const j=await r.json();if(!r.ok){$('status').textContent=j.error;return;}token=j.token;localStorage.setItem('chat-token',token);$('join').click();};\n$('join').onclick=async()=>{socket?.close();const room=$('room').value,r=await fetch('/rooms/'+room+'/messages',{headers:headers()});const j=await r.json();if(!r.ok){$('status').textContent=j.error;return;}$('messages').replaceChildren();j.messages.slice().reverse().forEach(append);socket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/rooms/'+room+'/ws?token='+encodeURIComponent(token));socket.onopen=()=>{$('status').textContent='Connected \u00b7 unread '+j.unread;};socket.onclose=()=>{$('status').textContent='Disconnected';};socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.error){$('status').textContent=m.error;return;}append(m);fetch('/rooms/'+room+'/read',{method:'POST',headers:headers()});};await fetch('/rooms/'+room+'/read',{method:'POST',headers:headers()});};\n$('send').onsubmit=e=>{e.preventDefault();if(socket?.readyState!==WebSocket.OPEN)return;socket.send(JSON.stringify({body:$('body').value}));$('body').value='';};\n</script></body></html>\n";
const MAX_CACHE = 1024 * 1024;
const encoder = new TextEncoder();
const htmlEscapes = {
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
};
const htmlSpecials = /[&<>"']/g;
const escapedCharacter = ch => htmlEscapes[ch];
const escapeHtml = value => value.replace(htmlSpecials, escapedCharacter);

function html(page, user) {
  const pieces = [`<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Room ${page.room_id}</title></head><body><header><h1>Room ${page.room_id}</h1><p>User: ${escapeHtml(user)}; unread: ${page.unread}; version: ${page.version}</p></header><main>`];
  for (const message of page.messages) {
    pieces.push(`<article data-id="${message.id}"><h2>${escapeHtml(message.user_name)}</h2><time>${message.created_at}</time><p>${escapeHtml(message.body)}</p></article>`);
  }
  pieces.push(`</main><form method="post" action="/rooms/${page.room_id}/messages"><textarea name="body"></textarea><button>Send</button></form></body></html>`);
  return pieces.join("");
}

export class ChatRoom {
  constructor(ctx) {
    this.ctx = ctx;
    this.cache = new Map();
    this.version = 0;
    this.bytes = 0;
    this.initialized = false;
  }

  reset(version) {
    this.version = version;
    this.cache.clear();
    this.bytes = 0;
  }

  initialize(room) {
    if (this.initialized) return;
    const sql=this.ctx.storage.sql;
    for (const statement of SCHEMA.schema) [...sql.exec(statement)];
    if (sql.exec("SELECT count(*) FROM room_meta").one()["count(*)"] === 0) {
      SCHEMA.seed.forEach((statement,index)=>[...sql.exec(statement,...(index>=2?[room]:[]))]);
    }
    [...sql.exec(SCHEMA.trigger)];
    this.initialized=true;
  }

  async fetch(request) {
    const path=new URL(request.url).pathname;
    const room=Number(path.split("/")[2]);
    if (Number(this.ctx.id.name) !== room) return Response.json({error:"not found"},{status:404});
    this.initialize(room);
    if (path.endsWith("/ws") || path.endsWith("/read")) {
      const error=(status,message)=>Response.json({error:message},{status});
      const authorization=request.headers.get("authorization") ?? "";
      if (!authorization.startsWith("Bearer ")) return error(401,"unauthorized");
      const token=authorization.slice(7);
      const user=[...this.ctx.storage.sql.exec(SQL.auth,token).raw()][0];
      if (!user) return error(401,"unauthorized");
      if (![...this.ctx.storage.sql.exec(SQL.member,user[0]).raw()].length) return error(403,"forbidden");
      if (path.endsWith("/ws")) {
        const [client,server]=Object.values(new WebSocketPair());
        this.ctx.acceptWebSocket(server,[token]);
        server.serializeAttachment({room,token});
        return new Response(null,{status:101,webSocket:client});
      }
      [...this.ctx.storage.sql.exec("UPDATE members SET unread=0 WHERE user_id=?",user[0])];
      [...this.ctx.storage.sql.exec("UPDATE room_meta SET version=version+1 WHERE id=1")];
      const version=this.ctx.storage.sql.exec(SQL.version).one().version;
      this.reset(version);
      return new Response('{"ok":true}',{headers:{"content-type":"application/json","x-chat-version":String(version),"x-chat-cache-hit":"0","x-chat-sql-calls":"5"}});
    }
    return this.renderOrPost(request);
  }

  webSocketMessage(socket,input) {
    const {room,token}=socket.deserializeAttachment();
    this.initialize(room);
    const query=(sql,...params)=>[...this.ctx.storage.sql.exec(sql,...params).raw()];
    const fail=error=>socket.send(JSON.stringify({error}));
    const user=query(SQL.auth,token)[0];
    if (!user) return fail("unauthorized");
    if (!query(SQL.member,user[0]).length) return fail("forbidden");
    let message;
    try { message=JSON.parse(typeof input==="string" ? input : new TextDecoder().decode(input)); } catch { return fail("invalid body"); }
    if (typeof message?.body!=="string" || !message.body.trim() || encoder.encode(message.body).length>8192) return fail("invalid body");
    this.post(query,room,user[0],message.body);
  }

  post(query,room,userId,body) {
    const inserted=query(SQL.insert,userId,body)[0];
    const version=query(SQL.version)[0][0];
    this.reset(version);
    const text=JSON.stringify({id:inserted[0],room_id:room,user_id:userId,body,created_at:inserted[1],version});
    for (const socket of this.ctx.getWebSockets()) socket.send(text);
    return {text,version};
  }

  webSocketClose(socket,code,reason) { socket.close(code,reason); }

  async renderOrPost(request) {
    let calls = 0;
    const query = (sql, ...params) => {
      calls++;
      return [...this.ctx.storage.sql.exec(sql, ...params).raw()];
    };
    const response = (status, body, json, version = 0, hit = false) => new Response(body, {
      status,
      headers: {
        "content-type": json ? "application/json" : "text/html; charset=utf-8",
        "x-chat-version": String(version),
        "x-chat-cache-hit": hit ? "1" : "0",
        "x-chat-sql-calls": String(calls),
      },
    });
    const error = (status, message) => response(status, JSON.stringify({ error: message }), true);
    const parts = new URL(request.url).pathname.split("/");
    const room = /^\d+$/.test(parts[2] ?? "") ? Number(parts[2]) : 0;
    if (parts[1] !== "rooms" || room < 1 || room > 50) return error(404, "not found");
    const json = parts[3] === "messages" && parts.length === 4;
    if (!(json || parts.length === 3) || !(request.method === "GET" || (json && request.method === "POST"))) return error(404, "not found");
    const authorization = request.headers.get("authorization") ?? "";
    if (!authorization.startsWith("Bearer ")) return error(401, "unauthorized");
    const user = query(SQL.auth, authorization.slice(7))[0];
    if (!user) return error(401, "unauthorized");
    const [userId, userName] = user;
    const member = query(SQL.member, userId)[0];
    if (!member) return error(403, "forbidden");
    const unread = member[0];

    if (request.method === "POST") {
      let message;
      try { message = JSON.parse(await request.text()); } catch { return error(400, "invalid body"); }
      if (typeof message?.body !== "string" || !message.body.trim() || encoder.encode(message.body).length > 8192) return error(400, "invalid body");
      const {text,version} = this.post(query,room,userId,message.body);
      return response(201, text, true, version);
    }

    const version = query(SQL.version)[0][0];
    if (this.version !== version) this.reset(version);
    const key = userId * 2 + Number(json);
    const cached = (request.headers.get("x-chat-cache") === "1") ? this.cache.get(key) : undefined;
    if (cached) return response(200, cached, json, version, true);
    const messages = query(SQL.messages).map(row => ({
      id: row[0], user_id: row[1], user_name: row[2], body: row[3], created_at: row[4],
    }));
    const page = { room_id: room, version, unread, messages };
    const body = encoder.encode(json ? JSON.stringify(page) : html(page, userName));
    if ((request.headers.get("x-chat-cache") === "1") && body.length <= MAX_CACHE) {
      if (this.bytes + body.length > MAX_CACHE) this.reset(version);
      this.bytes += body.length;
      this.cache.set(key, body);
    }
    return response(200, body, json, version);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" && request.method === "GET") return new Response(UI, {headers:{"content-type":"text/html; charset=utf-8"}});
    if (url.pathname === "/login" && request.method === "POST") {
      let login;
      try { login = JSON.parse(await request.text()); } catch { return Response.json({error:"unauthorized"},{status:401}); }
      if (login === null || !Number.isInteger(login.user) || login.user<1 || login.user>8 || login.password !== `password-${login.user}`) return Response.json({error:"unauthorized"},{status:401});
      return new Response(JSON.stringify({token:`token-${login.user}`}),{headers:{"content-type":"application/json"}});
    }
    const match = /^\/rooms\/(\d+)(?:\/(messages|read|ws))?$/.exec(url.pathname);
    const room = Number(match?.[1] ?? 0), suffix=match?.[2] ?? "";
    const allowed = suffix === "messages" ? ["GET","POST"].includes(request.method) : suffix === "read" ? request.method === "POST" : request.method === "GET";
    if (room<1 || room>50 || !allowed || (suffix==="ws" && request.headers.get("upgrade")?.toLowerCase()!=="websocket")) return Response.json({error:"not found"},{status:404});
    const headers = new Headers(request.headers);
    if (!headers.has("authorization") && url.searchParams.has("token")) headers.set("authorization", "Bearer "+url.searchParams.get("token"));
    return env.ROOMS.getByName(String(room)).fetch(new Request(request,{headers}));
  }
};

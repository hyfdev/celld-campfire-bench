import module from "./chat.wasm";

const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", {ignoreBOM: true});

class Writer {
  constructor(bytes = new Uint8Array(256), fixed = false) {
    this.bytes = bytes;
    this.fixed = fixed;
    this.offset = 0;
  }

  reserve(length) {
    if (this.offset + length <= this.bytes.length) return;
    if (this.fixed) throw new RangeError("Wasm SQL transfer buffer exceeded");
    const bytes = new Uint8Array(Math.max(this.offset + length, this.bytes.length * 2));
    bytes.set(this.bytes);
    this.bytes = bytes;
  }

  uint(value) {
    this.reserve(10);
    while (value >= 128) {
      this.bytes[this.offset++] = (value % 128) | 128;
      value = Math.floor(value / 128);
    }
    this.bytes[this.offset++] = value;
  }

  integer(value) {
    if (!Number.isSafeInteger(value * 2)) throw new RangeError("SQL integer exceeds the chat ABI range");
    this.uint(value >= 0 ? value * 2 : -value * 2 - 1);
  }

  data(bytes) {
    this.uint(bytes.length);
    this.reserve(bytes.length);
    this.bytes.set(bytes, this.offset);
    this.offset += bytes.length;
  }

  string(value) { this.data(encoder.encode(value)); }
  finish() { return this.bytes.subarray(0, this.offset); }
}

class Reader {
  constructor(bytes) {
    this.bytes = bytes;
    this.offset = 0;
  }

  uint() {
    let value = 0, factor = 1;
    for (;;) {
      if (this.offset >= this.bytes.length) throw new RangeError("Truncated chat ABI record");
      const byte = this.bytes[this.offset++];
      value += (byte & 127) * factor;
      if (!(byte & 128)) return value;
      factor *= 128;
    }
  }

  integer() {
    const value = this.uint();
    return value % 2 ? -(value + 1) / 2 : value / 2;
  }

  data() {
    const length = this.uint(), start = this.offset;
    this.offset += length;
    if (this.offset > this.bytes.length) throw new RangeError("Truncated chat ABI payload");
    return this.bytes.subarray(start, this.offset);
  }

  string() { return decoder.decode(this.data()); }
}

let active;
const instance = new WebAssembly.Instance(module, {chat: {
  input(ptr, cap) {
    if (ptr === 0 && cap === 0) return active.input.length;
    if (cap < active.input.length) throw new RangeError("Wasm request buffer too small");
    memory(ptr, active.input.length).set(active.input);
    return active.input.length;
  },
  sql_rows(room, sqlPtr, sqlLen, paramsPtr, paramsLen, out, cap) {
    if (!active.ctx || room !== Number(active.ctx.id.name)) throw new Error("Room storage capability mismatch");
    const sql = decoder.decode(memory(sqlPtr, sqlLen));
    const reader = new Reader(memory(paramsPtr, paramsLen));
    const params = [];
    for (let count = reader.uint(); count > 0; count--) {
      const kind = reader.uint();
      if (kind > 1) throw new Error("Unsupported chat SQL parameter");
      params.push(kind === 0 ? reader.integer() : reader.string());
    }
    const rows = [...active.ctx.storage.sql.exec(sql, ...params).raw()];
    const writer = new Writer(memory(out, cap), true);
    writer.uint(rows.length);
    for (const row of rows) {
      writer.uint(row.length);
      for (const value of row) {
        if (typeof value === "number") {
          writer.uint(0);
          writer.integer(value);
        } else if (typeof value === "string") {
          writer.uint(1);
          writer.string(value);
        } else throw new Error("Unsupported chat SQL value");
      }
    }
    return writer.offset;
  },
  output(ptr, len) {
    const reader = new Reader(memory(ptr, len));
    const kind = reader.uint();
    if (kind === 0) {
      active.action = {kind, status: reader.uint(), contentType: reader.string(),
        body: reader.data().slice(), version: reader.integer(),
        cacheHit: reader.uint() !== 0, sqlCalls: reader.uint()};
    } else if (kind === 1) active.action = {kind, room: reader.uint()};
    else if (kind === 2) active.action = {kind, socket: reader.uint()};
    else throw new Error("Unsupported chat action");
  },
  accept_socket(ptr, len) {
    const token = decoder.decode(memory(ptr, len));
    const [client, server] = Object.values(new WebSocketPair());
    active.ctx.acceptWebSocket(server, [token]);
    server.serializeAttachment({room: Number(active.ctx.id.name), token});
    active.client = client;
    return 1;
  },
  broadcast(ptr, len) {
    const text = decoder.decode(memory(ptr, len));
    for (const socket of active.ctx.getWebSockets()) socket.send(text);
  },
}});
instance.exports.init();

function memory(ptr, len) {
  return new Uint8Array(instance.exports.memory.buffer, ptr >>> 0, len >>> 0);
}

function call(kind, request, ctx) {
  const writer = new Writer();
  writer.string(request.method);
  writer.string(request.path);
  writer.string(request.authorization);
  writer.uint(Number(request.upgrade));
  writer.uint(Number(request.cache));
  writer.data(request.body);
  active = {input: writer.finish(), ctx};
  try {
    instance.exports.handle(kind);
    if (!active.action) throw new Error("Wasm returned no action");
    return {...active.action, client: active.client};
  } finally {
    active = undefined;
  }
}

async function input(request) {
  const url = new URL(request.url);
  return {
    method: request.method, path: url.pathname,
    authorization: request.headers.get("authorization") ??
      (url.searchParams.has("token") ? "Bearer " + url.searchParams.get("token") : ""),
    upgrade: request.headers.get("upgrade")?.toLowerCase() === "websocket",
    cache: request.headers.get("x-chat-cache") === "1",
    body: request.method === "POST" && (url.pathname === "/login" || url.pathname.endsWith("/messages"))
      ? new Uint8Array(await request.arrayBuffer()) : new Uint8Array(),
  };
}

function response(action) {
  if (action.kind === 2) return new Response(null, {status: 101, webSocket: action.client});
  if (action.kind !== 0) throw new Error("A room cannot forward its response");
  return new Response(action.body, {status: action.status, headers: {
    "content-type": action.contentType,
    "x-chat-version": String(action.version),
    "x-chat-cache-hit": action.cacheHit ? "1" : "0",
    "x-chat-sql-calls": String(action.sqlCalls),
  }});
}

export class ChatRoom {
  constructor(ctx) { this.ctx = ctx; }

  async fetch(request) {
    const record = await input(request);
    if (Number(record.path.split("/")[2]) !== Number(this.ctx.id.name)) {
      return Response.json({error: "not found"}, {status: 404});
    }
    return response(call(1, record, this.ctx));
  }

  webSocketMessage(socket, data) {
    const {room, token} = socket.deserializeAttachment();
    const action = call(1, {method: "POST", path: `/rooms/${room}/messages`,
      authorization: "Bearer " + token, upgrade: false, cache: false,
      body: typeof data === "string" ? encoder.encode(data) : new Uint8Array(data)}, this.ctx);
    if (action.kind !== 0) throw new Error("WebSocket message returned an invalid action");
    if (action.status !== 201) socket.send(decoder.decode(action.body));
  }

  webSocketClose(socket, code, reason) { socket.close(code, reason); }
}

export default {
  async fetch(request, env) {
    const record = await input(request);
    const action = call(0, record);
    if (action.kind !== 1) return response(action);
    const headers = new Headers(request.headers);
    if (!headers.has("authorization") && record.authorization) headers.set("authorization", record.authorization);
    return env.ROOMS.getByName(String(action.room)).fetch(new Request(request.url, {
      method: request.method, headers, body: record.body.length ? record.body : undefined,
    }));
  },
};

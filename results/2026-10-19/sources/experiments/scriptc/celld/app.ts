import { SQL, SCHEMA, UI } from "./data.ts";

declare function hostCall(operation: string, payload: string, output: Uint8Array): number;

type Cell = number | string;
interface Input {
  method: string;
  path: string;
  authorization: string;
  upgrade: boolean;
  cache: boolean;
  body: string;
}
interface Action {
  kind: string;
  room: number;
  socket: number;
  status: number;
  content_type: string;
  body: string;
  version: number;
  cache_hit: boolean;
  sql_calls: number;
}
interface Message { id: number; user_id: number; user_name: string; body: string; created_at: number; }
interface Page { room_id: number; version: number; unread: number; messages: Message[]; }

const output = new Uint8Array(4 * 1024 * 1024);
const decoder = new TextDecoder();
const encoder = new TextEncoder();
const htmlEscapes: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const htmlSpecials = /[&<>"']/g;
const escapedCharacter = (ch: string): string => htmlEscapes[ch];
const escapeHtml = (value: string): string => value.replace(htmlSpecials, escapedCharacter);

function rpc(operation: string, payload: string): string {
  const size = hostCall(operation, payload, output);
  if (size < 0 || size > output.length) throw new Error("host call failed");
  return decoder.decode(output.subarray(0, size));
}

function query(room: number, sql: string, params: Cell[] = []): Cell[][] {
  return JSON.parse(rpc("sql", JSON.stringify({room, sql, params})));
}

function action(kind: string, room = 0, socket = 0): Action {
  return {kind, room, socket, status: 200, content_type: "application/json", body: "", version: 0, cache_hit: false, sql_calls: 0};
}

function response(status: number, body: string, json = true, version = 0, cache_hit = false, sql_calls = 0): Action {
  return {kind: "response", room: 0, socket: 0, status, body, content_type: json ? "application/json" : "text/html; charset=utf-8", version, cache_hit, sql_calls};
}

function error(status: number, message: string, calls = 0): Action {
  return response(status, JSON.stringify({error: message}), true, 0, false, calls);
}

function html(page: Page, user: string): string {
  const pieces = [`<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Room ${page.room_id}</title></head><body><header><h1>Room ${page.room_id}</h1><p>User: ${escapeHtml(user)}; unread: ${page.unread}; version: ${page.version}</p></header><main>`];
  for (const message of page.messages) {
    pieces.push(`<article data-id="${message.id}"><h2>${escapeHtml(message.user_name)}</h2><time>${message.created_at}</time><p>${escapeHtml(message.body)}</p></article>`);
  }
  pieces.push(`</main><form method="post" action="/rooms/${page.room_id}/messages"><textarea name="body"></textarea><button>Send</button></form></body></html>`);
  return pieces.join("");
}

function route(request: Input): number {
  const match = /^\/rooms\/(\d+)(?:\/(messages|read|ws))?$/.exec(request.path);
  const room = Number(match?.[1] ?? 0);
  const suffix = match?.[2] ?? "";
  const allowed = suffix === "messages" ? request.method === "GET" || request.method === "POST" : suffix === "read" ? request.method === "POST" : request.method === "GET";
  return room >= 1 && room <= 50 && allowed && (suffix !== "ws" || request.upgrade) ? room : 0;
}

function ingress(request: Input): Action {
  if (request.path === "/" && request.method === "GET") return response(200, UI, false);
  if (request.path === "/login" && request.method === "POST") {
    try {
      const login = JSON.parse(request.body);
      if (login === null || !Number.isInteger(login.user) || login.user < 1 || login.user > 8 || login.password !== `password-${login.user}`) return error(401, "unauthorized");
      return response(200, JSON.stringify({token: `token-${login.user}`}));
    } catch { return error(401, "unauthorized"); }
  }
  const room = route(request);
  return room === 0 ? error(404, "not found") : action("forward", room);
}

class Room {
  room: number;
  initialized = false;
  version = 0;
  bytes = 0;
  cache = new Map<number, string>();

  constructor(room: number) { this.room = room; }

  reset(version: number): void {
    this.version = version;
    this.bytes = 0;
    this.cache.clear();
  }

  initialize(): void {
    if (this.initialized) return;
    for (const sql of SCHEMA.schema) query(this.room, sql);
    if (Number(query(this.room, "SELECT count(*) FROM room_meta")[0][0]) === 0) {
      for (let index = 0; index < SCHEMA.seed.length; index++) query(this.room, SCHEMA.seed[index], index >= 2 ? [this.room] : []);
    }
    query(this.room, SCHEMA.trigger);
    this.initialized = true;
  }

  handle(request: Input): Action {
    this.initialize();
    let calls = 0;
    const q = (sql: string, params: Cell[] = []): Cell[][] => { calls++; return query(this.room, sql, params); };
    const fail = (status: number, message: string): Action => error(status, message, calls);
    if (!request.authorization.startsWith("Bearer ")) return fail(401, "unauthorized");
    const token = request.authorization.slice(7);
    const user = q(SQL.auth, [token])[0];
    if (!user) return fail(401, "unauthorized");
    const userId = Number(user[0]);
    const member = q(SQL.member, [userId])[0];
    if (!member) return fail(403, "forbidden");
    if (request.path.endsWith("/ws")) {
      return action("upgrade", this.room, Number(rpc("accept", token)));
    }
    if (request.path.endsWith("/read")) {
      q("UPDATE members SET unread=0 WHERE user_id=?", [userId]);
      q("UPDATE room_meta SET version=version+1 WHERE id=1");
      const version = Number(q(SQL.version)[0][0]);
      this.reset(version);
      return response(200, '{"ok":true}', true, version, false, calls);
    }
    if (request.method === "POST") {
      let body = "";
      try {
        const message = JSON.parse(request.body);
        if (message === null || typeof message.body !== "string") return fail(400, "invalid body");
        body = message.body;
      } catch { return fail(400, "invalid body"); }
      if (!body.trim() || encoder.encode(body).length > 8192) return fail(400, "invalid body");
      const inserted = q(SQL.insert, [userId, body])[0];
      const version = Number(q(SQL.version)[0][0]);
      this.reset(version);
      const text = JSON.stringify({id: Number(inserted[0]), room_id: this.room, user_id: userId, body, created_at: Number(inserted[1]), version});
      rpc("broadcast", text);
      return response(201, text, true, version, false, calls);
    }
    const version = Number(q(SQL.version)[0][0]);
    if (this.version !== version) this.reset(version);
    const json = request.path.endsWith("/messages");
    const key = userId * 2 + Number(json);
    const cached = request.cache ? this.cache.get(key) : undefined;
    if (cached) return response(200, cached, json, version, true, calls);
    const messages = q(SQL.messages).map(row => ({id: Number(row[0]), user_id: Number(row[1]), user_name: String(row[2]), body: String(row[3]), created_at: Number(row[4])}));
    const page: Page = {room_id: this.room, version, unread: Number(member[0]), messages};
    const body = json ? JSON.stringify(page) : html(page, String(user[1]));
    const bytes = encoder.encode(body).length;
    if (request.cache && bytes <= 1024 * 1024) {
      if (this.bytes + bytes > 1024 * 1024) this.reset(version);
      this.bytes += bytes;
      this.cache.set(key, body);
    }
    return response(200, body, json, version, false, calls);
  }
}

const rooms = new Map<number, Room>();

export function handle(kind: number, input: string): string {
  const request: Input = JSON.parse(input);
  if (kind === 0) return JSON.stringify(ingress(request));
  const room = route(request);
  if (room === 0) return JSON.stringify(error(404, "not found"));
  let object = rooms.get(room);
  if (!object) { object = new Room(room); rooms.set(room, object); }
  return JSON.stringify(object.handle(request));
}

export function release(room: number): void { rooms.delete(room); }

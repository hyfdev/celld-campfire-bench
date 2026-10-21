#include "native-wasm.h"

#include <workerd/api/actor.h>
#include <workerd/api/hibernatable-web-socket.h>
#include <workerd/api/sql.h>
#include <workerd/io/actor-cache.h>
#include <workerd/io/legacy-hibernation-manager.h>
#include <workerd/util/sqlite.h>
#include <workerd/util/stream-utils.h>

#include <strings.h>

#include <kj/compat/url.h>
#include <kj/encoding.h>

#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <list>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

namespace workerd {
namespace {

using Bytes = std::vector<byte>;

v8::Local<v8::String> string(v8::Isolate* isolate, kj::StringPtr text) {
  return v8::String::NewFromUtf8(
      isolate, text.begin(), v8::NewStringType::kNormal, static_cast<int>(text.size()))
      .ToLocalChecked();
}

v8::Local<v8::Value> get(jsg::Lock& js, v8::Local<v8::Object> object, kj::StringPtr name) {
  return jsg::check(object->Get(js.v8Context(), string(js.v8Isolate, name)));
}

kj::Maybe<kj::StringPtr> header(const kj::HttpHeaders& headers, kj::StringPtr wanted) {
  kj::Maybe<kj::StringPtr> result;
  headers.forEach([&](kj::StringPtr name, kj::StringPtr value) {
    if (strcasecmp(name.cStr(), wanted.cStr()) == 0) result = value;
  });
  return result;
}

struct Writer {
  Bytes bytes;

  void uint(uint64_t value) {
    do {
      byte b = value & 127;
      value >>= 7;
      bytes.push_back(b | (value ? 128 : 0));
    } while (value);
  }
  void integer(int64_t value) {
    uint((static_cast<uint64_t>(value) << 1) ^ static_cast<uint64_t>(value >> 63));
  }
  void data(kj::ArrayPtr<const byte> value) {
    uint(value.size());
    if (value.size() > 0) bytes.insert(bytes.end(), value.begin(), value.end());
  }
  void text(kj::StringPtr value) {
    data(value.asBytes());
  }
};

struct Reader {
  kj::ArrayPtr<const byte> bytes;
  size_t offset = 0;

  uint64_t uint() {
    uint64_t value = 0;
    for (unsigned shift = 0; shift < 70; shift += 7) {
      KJ_REQUIRE(offset < bytes.size(), "Truncated chat ABI record");
      byte b = bytes[offset++];
      KJ_REQUIRE(shift < 63 || b < 2, "Chat ABI integer overflow");
      value |= static_cast<uint64_t>(b & 127) << shift;
      if (!(b & 128)) return value;
    }
    KJ_FAIL_REQUIRE("Invalid chat ABI integer");
  }
  int64_t integer() {
    uint64_t value = uint();
    return static_cast<int64_t>(value >> 1) ^ -static_cast<int64_t>(value & 1);
  }
  kj::ArrayPtr<const byte> data() {
    uint64_t n = uint();
    KJ_REQUIRE(n <= bytes.size() - offset, "Truncated chat ABI payload");
    auto value = bytes.slice(offset, offset + n);
    offset += n;
    return value;
  }
  kj::String text() {
    auto value = data();
    return kj::str(value.asChars());
  }
  void finish() {
    KJ_REQUIRE(offset == bytes.size(), "Trailing chat ABI bytes");
  }
};

struct Request {
  kj::String method;
  kj::String path;
  kj::String authorization;
  bool upgrade = false;
  bool cache = false;
  kj::Array<byte> body;

  Bytes encode() const {
    Writer w;
    w.text(method);
    w.text(path);
    w.text(authorization);
    w.uint(upgrade);
    w.uint(cache);
    w.data(body);
    return kj::mv(w.bytes);
  }
};

struct Action {
  uint kind = 0;
  uint status = 0;
  kj::String contentType;
  Bytes body;
  int64_t version = 0;
  bool cacheHit = false;
  uint sqlCalls = 0;
  uint room = 0;
  kj::Maybe<jsg::Ref<api::WebSocket>> socket;
  kj::Maybe<kj::Own<WorkerInterface>> forward;
};

class Regulator final: public api::SqlStorageRegulator {
 public:
  void onError(kj::Maybe<int>, kj::StringPtr message) const override {
    KJ_FAIL_REQUIRE(message);
  }
  bool allowTransactions() const override {
    KJ_FAIL_REQUIRE("Explicit transactions are disabled");
  }
};

const Regulator regulator;

kj::StringPtr statusText(uint status) {
  switch (status) {
    case 200:
      return "OK";
    case 201:
      return "Created";
    case 400:
      return "Bad Request";
    case 401:
      return "Unauthorized";
    case 403:
      return "Forbidden";
    case 404:
      return "Not Found";
    case 413:
      return "Payload Too Large";
    default:
      return "Error";
  }
}

}  // namespace

struct CampfireRoom::Impl {
  struct Statement {
    SqliteDatabase::Statement statement;
    std::list<std::string>::iterator lru;
  };

  Worker::Actor& actor;
  CampfireWasm& wasm;
  uint room;
  SqliteDatabase& db;
  std::list<std::string> lru;
  std::unordered_map<std::string, Statement> statements;
  size_t statementBytes = 0;

  Impl(Worker::Actor& actor, CampfireWasm& wasm)
      : actor(actor),
        wasm(wasm),
        room(KJ_REQUIRE_NONNULL(actor.getId().get<kj::Own<ActorIdFactory::ActorId>>()->getName())
                 .parseAs<uint>()),
        db(KJ_REQUIRE_NONNULL(KJ_REQUIRE_NONNULL(actor.getPersistent()).getSqliteDatabase())) {}

  SqliteDatabase::Statement& prepare(kj::StringPtr sql) {
    std::string key(sql.begin(), sql.size());
    auto found = statements.find(key);
    if (found != statements.end()) {
      lru.splice(lru.end(), lru, found->second.lru);
      return found->second.statement;
    }
    while (!lru.empty() && statementBytes + sql.size() > 1024 * 1024) {
      statementBytes -= lru.front().size();
      statements.erase(lru.front());
      lru.pop_front();
    }
    lru.push_back(key);
    statementBytes += sql.size();
    return statements
        .emplace(
            kj::mv(key), Statement{db.prepareMulti(regulator, kj::str(sql)), std::prev(lru.end())})
        .first->second.statement;
  }
};

struct CampfireWasm::Impl {
  jsg::Ref<api::DurableObjectNamespace> rooms;
  v8::Global<v8::WasmMemoryObject> memory;
  v8::Global<v8::Function> handle;
  v8::Global<v8::Function> release;
  jsg::Lock* js = nullptr;
  CampfireRoom::Impl* room = nullptr;
  Bytes input;
  kj::Maybe<Action> output;
  kj::Maybe<jsg::Ref<api::WebSocket>> socket;
  std::vector<uint> releases;

  static jsg::Ref<api::DurableObjectNamespace> namespaceBinding(
      jsg::Lock& js, v8::Local<v8::Value> env) {
    auto value = get(js, env.As<v8::Object>(), "ROOMS");
    const auto& handler =
        KJ_REQUIRE_NONNULL(js.tryGetTypeHandler<jsg::Ref<api::DurableObjectNamespace>>());
    auto binding = handler.tryUnwrap(js, value);
    return kj::mv(KJ_REQUIRE_NONNULL(binding));
  }

  Impl(jsg::Lock& js, v8::Local<v8::Value> env): rooms(namespaceBinding(js, env)) {
    jsg::Lock::AllowEvalScope allow(js, true);
    std::ifstream file(std::getenv("WORKERD_CHAT_WASM"), std::ios::binary);
    KJ_REQUIRE(file.good(), "Cannot open WORKERD_CHAT_WASM");
    Bytes bytes((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    auto module = jsg::check(v8::WasmModuleObject::Compile(
        js.v8Isolate, std::span<const uint8_t>(bytes.data(), bytes.size())));
    auto imports = v8::Object::New(js.v8Isolate);
    auto chat = v8::Object::New(js.v8Isolate);
    auto data = v8::External::New(js.v8Isolate, this, v8::kExternalPointerTypeTagDefault);
    auto bind = [&](kj::StringPtr name, v8::FunctionCallback callback) {
      auto fn = jsg::check(v8::Function::New(js.v8Context(), callback, data));
      KJ_REQUIRE(chat->Set(js.v8Context(), string(js.v8Isolate, name), fn).FromMaybe(false));
    };
    bind("input", inputCallback);
    bind("output", outputCallback);
    bind("sql_rows", sqlCallback);
    bind("accept_socket", socketCallback);
    bind("broadcast", broadcastCallback);
    KJ_REQUIRE(imports->Set(js.v8Context(), string(js.v8Isolate, "chat"), chat).FromMaybe(false));
    auto webassembly = get(js, js.v8Context()->Global(), "WebAssembly").As<v8::Object>();
    auto constructor = get(js, webassembly, "Instance").As<v8::Function>();
    v8::Local<v8::Value> args[] = {module, imports};
    auto instance = jsg::check(constructor->NewInstance(js.v8Context(), 2, args));
    auto exports = get(js, instance, "exports").As<v8::Object>();
    memory.Reset(js.v8Isolate, get(js, exports, "memory").As<v8::WasmMemoryObject>());
    handle.Reset(js.v8Isolate, get(js, exports, "handle").As<v8::Function>());
    release.Reset(js.v8Isolate, get(js, exports, "release").As<v8::Function>());
    invoke(js, get(js, exports, "init").As<v8::Function>(), kj::none);
  }

  void invoke(jsg::Lock& lock, v8::Local<v8::Function> fn, kj::Maybe<int> value) {
    v8::TryCatch catcher(lock.v8Isolate);
    v8::Local<v8::Value> arg = v8::Integer::New(lock.v8Isolate, value.orDefault(0));
    v8::Local<v8::Value> result;
    if (!fn->Call(lock.v8Context(), v8::Undefined(lock.v8Isolate), value == kj::none ? 0 : 1, &arg)
             .ToLocal(&result)) {
      v8::String::Utf8Value error(lock.v8Isolate, catcher.Exception());
      KJ_FAIL_REQUIRE("Direct Wasm execution failed", *error == nullptr ? "Wasm trap" : *error);
    }
  }

  Action call(jsg::Lock& lock, int kind, const Request& request, CampfireRoom::Impl* current) {
    KJ_REQUIRE(js == nullptr, "Reentrant Wasm execution");
    for (uint id: releases) invoke(lock, release.Get(lock.v8Isolate), static_cast<int>(id));
    releases.clear();
    js = &lock;
    room = current;
    input = request.encode();
    output = kj::none;
    socket = kj::none;
    KJ_DEFER({
      js = nullptr;
      room = nullptr;
      input.clear();
      output = kj::none;
      socket = kj::none;
    });
    invoke(lock, handle.Get(lock.v8Isolate), kind);
    auto action = kj::mv(KJ_REQUIRE_NONNULL(output, "Wasm returned no action"));
    if (action.kind == 1) {
      KJ_REQUIRE(current == nullptr, "A room cannot forward its response");
      action.forward = rooms->startNativeRequest(kj::str(action.room));
    } else if (action.kind == 2) {
      action.socket = kj::mv(KJ_REQUIRE_NONNULL(socket));
    }
    return action;
  }

  kj::ArrayPtr<byte> buffer(uint32_t ptr, int32_t length) {
    KJ_REQUIRE(js != nullptr && length >= 0, "Invalid Wasm callback");
    auto backing = memory.Get(js->v8Isolate)->Buffer()->GetBackingStore();
    KJ_REQUIRE(ptr <= backing->ByteLength() &&
            static_cast<uint32_t>(length) <= backing->ByteLength() - ptr,
        "Wasm buffer is outside linear memory");
    return {static_cast<byte*>(backing->Data()) + ptr, static_cast<size_t>(length)};
  }

  template <typename Func>
  static void callback(const v8::FunctionCallbackInfo<v8::Value>& args, Func&& func) {
    auto& self = *static_cast<Impl*>(
        args.DataV2().As<v8::External>()->Value(v8::kExternalPointerTypeTagDefault));
    KJ_IF_SOME(error, kj::runCatchingExceptions([&]() { func(self); })) {
      args.GetIsolate()->ThrowException(
          v8::Exception::Error(string(args.GetIsolate(), error.getDescription())));
    }
  }

  static int32_t arg(const v8::FunctionCallbackInfo<v8::Value>& args, int n) {
    KJ_REQUIRE(n < args.Length() && args[n]->IsInt32(), "Invalid Wasm import argument");
    return args[n].As<v8::Int32>()->Value();
  }

  static void inputCallback(const v8::FunctionCallbackInfo<v8::Value>& args) {
    callback(args, [&](Impl& self) {
      int32_t ptr = arg(args, 0), cap = arg(args, 1);
      if (ptr != 0 || cap != 0) {
        auto out = self.buffer(ptr, cap);
        KJ_REQUIRE(out.size() >= self.input.size(), "Wasm input buffer is too small");
        std::memcpy(out.begin(), self.input.data(), self.input.size());
      }
      args.GetReturnValue().Set(static_cast<int32_t>(self.input.size()));
    });
  }

  static void outputCallback(const v8::FunctionCallbackInfo<v8::Value>& args) {
    callback(args, [&](Impl& self) {
      KJ_REQUIRE(self.output == kj::none, "Duplicate Wasm response");
      Reader r{self.buffer(arg(args, 0), arg(args, 1))};
      Action action;
      action.kind = r.uint();
      if (action.kind == 0) {
        action.status = r.uint();
        action.contentType = r.text();
        auto body = r.data();
        action.body.assign(body.begin(), body.end());
        action.version = r.integer();
        action.cacheHit = r.uint();
        action.sqlCalls = r.uint();
      } else if (action.kind == 1) {
        action.room = r.uint();
        r.data();
        r.data();
        r.data();
        r.uint();
        r.uint();
        r.data();
      } else {
        KJ_REQUIRE(action.kind == 2 && r.uint() == 1, "Invalid Wasm socket response");
      }
      r.finish();
      self.output = kj::mv(action);
    });
  }

  static void sqlCallback(const v8::FunctionCallbackInfo<v8::Value>& args) {
    callback(args, [&](Impl& self) {
      KJ_REQUIRE(self.room != nullptr && static_cast<uint32_t>(arg(args, 0)) == self.room->room,
          "Room storage capability mismatch");
      auto rawSql = self.buffer(arg(args, 1), arg(args, 2));
      auto sql = kj::str(rawSql.asChars());
      Reader reader{self.buffer(arg(args, 3), arg(args, 4))};
      uint64_t count = reader.uint();
      KJ_REQUIRE(count <= 100, "Too many SQL bindings");
      std::vector<SqliteDatabase::Query::ValuePtr> params;
      std::vector<kj::String> strings;
      params.reserve(count);
      strings.reserve(count);
      for (uint64_t i = 0; i < count; ++i) {
        auto kind = reader.uint();
        if (kind == 0)
          params.emplace_back(static_cast<double>(reader.integer()));
        else {
          KJ_REQUIRE(kind == 1, "Unsupported SQL binding");
          strings.push_back(reader.text());
          params.emplace_back(strings.back().asPtr());
        }
      }
      reader.finish();
      auto& room = *self.room;
      auto query = room.prepare(sql).run(
          SqliteDatabase::Statement::StatementOptions{.queryId = room.db.generateQueryId()},
          kj::ArrayPtr<const SqliteDatabase::Query::ValuePtr>(params.data(), params.size()));
      Writer rows;
      uint64_t rowCount = 0;
      while (!query.isDone()) {
        rows.uint(query.columnCount());
        for (uint i = 0; i < query.columnCount(); ++i) {
          auto value = query.getValue(i);
          KJ_SWITCH_ONEOF(value) {
            KJ_CASE_ONEOF(text, kj::StringPtr) {
              rows.uint(1);
              rows.text(text);
            }
            KJ_CASE_ONEOF(integer, int64_t) {
              rows.uint(0);
              rows.integer(integer);
            }
            KJ_CASE_ONEOF(number, double) {
              KJ_REQUIRE(number == static_cast<int64_t>(number), "Noninteger chat SQL value");
              rows.uint(0);
              rows.integer(static_cast<int64_t>(number));
            }
            KJ_CASE_ONEOF(blob, kj::ArrayPtr<const byte>) {
              KJ_FAIL_REQUIRE("Unexpected SQL blob");
            }
            KJ_CASE_ONEOF(null, decltype(nullptr)) {
              KJ_FAIL_REQUIRE("Unexpected SQL null");
            }
          }
        }
        ++rowCount;
        query.nextRow();
      }
      Writer prefix;
      prefix.uint(rowCount);
      auto out = self.buffer(arg(args, 5), arg(args, 6));
      KJ_REQUIRE(
          prefix.bytes.size() + rows.bytes.size() <= out.size(), "SQL transfer buffer exceeded");
      std::memcpy(out.begin(), prefix.bytes.data(), prefix.bytes.size());
      if (!rows.bytes.empty())
        std::memcpy(out.begin() + prefix.bytes.size(), rows.bytes.data(), rows.bytes.size());
      args.GetReturnValue().Set(static_cast<int32_t>(prefix.bytes.size() + rows.bytes.size()));
    });
  }

  static void socketCallback(const v8::FunctionCallbackInfo<v8::Value>& args) {
    callback(args, [&](Impl& self) {
      KJ_REQUIRE(self.room != nullptr && self.socket == kj::none, "Invalid socket accept");
      auto token = self.buffer(arg(args, 0), arg(args, 1));
      auto pair = api::WebSocketPair::constructor(*self.js);
      auto& actor = self.room->actor;
      if (actor.getHibernationManager() == kj::none) {
        actor.setHibernationManager(kj::refcounted<LegacyHibernationManagerImpl>(
            actor.getLoopback(), KJ_REQUIRE_NONNULL(actor.getHibernationEventType())));
      }
      auto tag = kj::str(token.asChars());
      auto tags = kj::arr(kj::mv(tag));
      KJ_REQUIRE_NONNULL(actor.getHibernationManager()).acceptWebSocket(pair->getSecond(), tags);
      self.socket = pair->getFirst();
      args.GetReturnValue().Set(1);
    });
  }

  static void broadcastCallback(const v8::FunctionCallbackInfo<v8::Value>& args) {
    callback(args, [&](Impl& self) {
      KJ_REQUIRE(self.room != nullptr, "Broadcast outside a room");
      auto data = self.buffer(arg(args, 0), arg(args, 1));
      KJ_IF_SOME(manager, self.room->actor.getHibernationManager()) {
        auto sockets = manager.getWebSockets(*self.js, kj::none);
        for (auto& socket: sockets) {
          socket->send(*self.js, kj::str(data.asChars()));
        }
      }
    });
  }
};

bool CampfireWasm::enabled() {
  static bool value = std::getenv("WORKERD_CHAT_WASM") != nullptr;
  return value;
}

CampfireWasm::CampfireWasm(jsg::Lock& js, v8::Local<v8::Value> env)
    : impl(kj::heap<Impl>(js, env)) {}
CampfireWasm::~CampfireWasm() noexcept(false) = default;
CampfireRoom::CampfireRoom(Worker::Actor& actor, CampfireWasm& wasm)
    : impl(kj::heap<Impl>(actor, wasm)) {}
CampfireRoom::~CampfireRoom() noexcept(false) {
  impl->wasm.getImpl().releases.push_back(impl->room);
}

kj::Promise<api::DeferredProxy<void>> CampfireWasm::request(IoContext& context,
    kj::HttpMethod method,
    kj::StringPtr url,
    const kj::HttpHeaders& headers,
    kj::AsyncInputStream& body,
    kj::HttpService::Response& response) {
  auto parsed =
      kj::Url::parse(url, url.startsWith("/") ? kj::Url::HTTP_REQUEST : kj::Url::REMOTE_HREF);
  auto question = url.findFirst('?').orDefault(url.size());
  auto scheme = url.findFirst(':');
  size_t start = 0;
  KJ_IF_SOME(colon, scheme) {
    auto slash = url.slice(colon + 3).findFirst('/');
    start = slash.map([&](size_t i) { return colon + 3 + i; }).orDefault(question);
  }
  auto path = start < question ? kj::str(url.slice(start, question)) : kj::str("/");
  auto authorization =
      header(headers, "authorization").map([](kj::StringPtr v) { return kj::str(v); });
  if (authorization == kj::none) {
    for (auto& q: parsed.query) {
      if (q.name == "token") {
        authorization = kj::str("Bearer ", q.value);
        break;
      }
    }
  }
  Request request{.method = kj::str(method),
    .path = kj::mv(path),
    .authorization = kj::mv(authorization).orDefault(kj::str("")),
    .upgrade = headers.isWebSocket(),
    .cache = header(headers, "x-chat-cache").orDefault("") == "1"_kj};
  if (method == kj::HttpMethod::POST &&
      (request.path == "/login" || request.path.endsWith("/messages"))) {
    request.body = co_await body.readAllBytes(1024 * 1024);
  }
  auto action = co_await context.run([&](Worker::Lock& lock) {
    auto& wasm = lock.getWorker().getCampfireWasm(lock);
    CampfireRoom::Impl* room = nullptr;
    KJ_IF_SOME(actor, context.getActor()) room = &actor.getCampfireRoom(lock).getImpl();
    context.getMetrics().claimRetryTokenBeforeUserCode(IsRetryableHandler::NO);
    return wasm.getImpl().call(lock, room == nullptr ? 0 : 1, request, room);
  });
  if (action.kind == 1) {
    auto forward = kj::mv(KJ_REQUIRE_NONNULL(action.forward));
    kj::HttpHeaders forwardedHeaders(context.getHeaderTable());
    headers.forEach([&](kj::StringPtr name, kj::StringPtr value) {
      if (strcasecmp(name.cStr(), "authorization") != 0) forwardedHeaders.addPtrPtr(name, value);
    });
    forwardedHeaders.addPtrPtr("authorization", request.authorization);
    auto forwardedBody = newMemoryInputStream(request.body);
    auto pending = context.registerPendingEvent();
    KJ_CO_MAGIC api::BEGIN_DEFERRED_PROXYING;
    co_await forward->request(method, url, forwardedHeaders, *forwardedBody, response);
    co_return;
  }
  co_await context.waitForOutputLocks();
  if (action.kind == 2) {
    auto proxy = co_await context.run([&](Worker::Lock& lock) {
      kj::HttpHeaders out(context.getHeaderTable());
      auto transport = response.acceptWebSocket(out);
      auto result =
          KJ_REQUIRE_NONNULL(action.socket)->couple(lock, kj::mv(transport), context.getMetrics());
      auto& manager = KJ_REQUIRE_NONNULL(context.getActorOrThrow().getHibernationManager());
      return result.attach(kj::addRef(manager));
    });
    KJ_CO_MAGIC api::BEGIN_DEFERRED_PROXYING;
    co_await proxy.proxyTask;
    co_return;
  }
  kj::HttpHeaders out(context.getHeaderTable());
  out.setPtr(kj::HttpHeaderId::CONTENT_TYPE, action.contentType);
  out.addPtr("x-chat-version", kj::str(action.version));
  out.addPtrPtr("x-chat-cache-hit", action.cacheHit ? "1" : "0");
  out.addPtr("x-chat-sql-calls", kj::str(action.sqlCalls));
  auto stream = response.send(action.status, statusText(action.status), out, action.body.size());
  KJ_CO_MAGIC api::BEGIN_DEFERRED_PROXYING;
  if (method != kj::HttpMethod::HEAD)
    co_await stream->write(kj::arrayPtr(action.body.data(), action.body.size()));
}

void CampfireWasm::webSocketEvent(Worker::Lock& lock, api::HibernatableSocketParams& params) {
  auto& context = IoContext::current();
  jsg::Lock& js = lock;
  auto event = js.alloc<api::HibernatableWebSocketEvent>();
  KJ_SWITCH_ONEOF(params.eventType) {
    KJ_CASE_ONEOF(close, api::HibernatableSocketParams::Close) {
      auto package = event->prepareForRelease(lock, params.websocketId);
      package.webSocketRef->initiateHibernatableRelease(
          lock, kj::mv(package.tags), api::WebSocket::HibernatableReleaseState::CLOSE);
      package.webSocketRef->close(lock, close.code, jsg::USVString(kj::str(close.reason)));
      return;
    }
    KJ_CASE_ONEOF(error, api::HibernatableSocketParams::Error) {
      auto package = event->prepareForRelease(lock, params.websocketId);
      package.webSocketRef->initiateHibernatableRelease(
          lock, kj::mv(package.tags), api::WebSocket::HibernatableReleaseState::ERROR);
      return;
    }
    KJ_CASE_ONEOF(text, api::HibernatableSocketParams::Text) {}
    KJ_CASE_ONEOF(data, api::HibernatableSocketParams::Data) {}
  }
  auto socket = event->claimWebSocket(lock, params.websocketId);
  auto tags = socket->getHibernatableTags();
  KJ_REQUIRE(tags.size() == 1, "Socket authentication tag missing");
  auto& room = context.getActorOrThrow().getCampfireRoom(lock).getImpl();
  kj::Array<byte> body;
  KJ_IF_SOME(text, params.eventType.tryGet<api::HibernatableSocketParams::Text>()) {
    body = kj::heapArray<byte>(text.message.asBytes());
  } else {
    body = kj::mv(params.eventType.get<api::HibernatableSocketParams::Data>().message);
  }
  Request request{.method = kj::str("POST"),
    .path = kj::str("/rooms/", room.room, "/messages"),
    .authorization = kj::str("Bearer ", tags[0]),
    .body = kj::mv(body)};
  auto action = lock.getWorker().getCampfireWasm(lock).getImpl().call(lock, 1, request, &room);
  KJ_REQUIRE(action.kind == 0, "Invalid WebSocket application response");
  if (action.status != 201) {
    socket->send(lock, kj::str(kj::arrayPtr(action.body.data(), action.body.size()).asChars()));
  }
}

}  // namespace workerd

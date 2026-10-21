#pragma once

#include <workerd/api/deferred-proxy.h>
#include <workerd/api/hibernation-event-params.h>
#include <workerd/io/io-context.h>
#include <workerd/io/worker.h>

namespace workerd {

class CampfireWasm {
 public:
  struct Impl;

  CampfireWasm(jsg::Lock& js, v8::Local<v8::Value> env);
  ~CampfireWasm() noexcept(false);
  KJ_DISALLOW_COPY_AND_MOVE(CampfireWasm);

  static bool enabled();
  static kj::Promise<api::DeferredProxy<void>> request(IoContext& context,
      kj::HttpMethod method,
      kj::StringPtr url,
      const kj::HttpHeaders& headers,
      kj::AsyncInputStream& body,
      kj::HttpService::Response& response);
  static void webSocketEvent(Worker::Lock& lock, api::HibernatableSocketParams& params);

  Impl& getImpl() {
    return *impl;
  }

 private:
  kj::Own<Impl> impl;
};

class CampfireRoom {
 public:
  struct Impl;

  CampfireRoom(Worker::Actor& actor, CampfireWasm& wasm);
  ~CampfireRoom() noexcept(false);
  KJ_DISALLOW_COPY_AND_MOVE(CampfireRoom);

  Impl& getImpl() {
    return *impl;
  }

 private:
  kj::Own<Impl> impl;
};

}  // namespace workerd

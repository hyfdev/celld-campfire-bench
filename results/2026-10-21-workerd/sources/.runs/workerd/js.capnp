using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name = "chat", worker = (
      modules = [(name = "worker.js", esModule = embed "worker.js")],
      compatibilityDate = "2026-09-01",
      bindings = [(name = "ROOMS", durableObjectNamespace = "ChatRoom")],
      durableObjectNamespaces = [(
        className = "ChatRoom", uniqueKey = "campfire-workerd-bench", enableSql = true
      )],
      durableObjectStorage = (localDisk = "storage")
    )),
    (name = "storage", disk = (path = "state", writable = true))
  ],
  sockets = [(name = "http", address = "127.0.0.1:19880", http = (), service = "chat")]
);

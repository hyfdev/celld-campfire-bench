# celld-campfire-bench

A reproducible comparison of **JavaScript, TypeScript compiled to Wasm with scriptc, Rust Wasm, and native Rust** running the same chat workload inside [celld](https://github.com/denoland/celld). A stateless Worker routes requests to one Durable Object per chat room; each object stores messages in SQLite.

**Celld itself is a native Rust executable in every mode. The Wasm variants compile application code to Wasm.**

## TypeScript → Wasm (scriptc)

[scriptc](https://github.com/vercel-labs/scriptc) compiles the TypeScript Worker and Durable Object handlers to Wasm. Wasmtime runs them inside celld; Rust implements their SQLite and WebSocket imports. Request handling and database access execute without JavaScript.

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64 · 2026-10-19. Medians of three 3-second trials, 32 simultaneous clients. All ratios use JavaScript as the baseline.

### Server-rendered HTML (without in-memory page caching)

| Application runtime | Requests/s | CPU use (%; 100% = 1 core) | CPU µs/request | Peak process memory (MiB) |
| --- | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 2,488 (1.00×) | 114.3 (1.00×) | 459.5 (1.00×) | 111.5 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 1,635 (0.66×) | 106.8 (0.93×) | 655.6 (1.43×) | 64.6 (0.58×) |
| Native Rust | 10,595 (4.26×) | 138.9 (1.22×) | 131.1 (0.29×) | 44.8 (0.40×) |

### Read messages as JSON (no response cache)

| Application runtime | Requests/s | CPU use (%; 100% = 1 core) | CPU µs/request | Peak process memory (MiB) |
| --- | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 3,050 (1.00×) | 116.1 (1.00×) | 380.9 (1.00×) | 109.7 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 5,199 (1.70×) | 120.3 (1.04×) | 233.0 (0.61×) | 67.0 (0.61×) |
| Native Rust | 13,224 (4.34×) | 148.8 (1.28×) | 112.2 (0.29×) | 46.2 (0.42×) |

CPU and memory cover the whole celld process. Each request queries SQLite and returns the latest 50 messages. Startup and module compilation precede load timing.

[All seven workloads, trial ranges, and raw measurements](experiments/scriptc/results/celld/REPORT.md).

<!-- benchmark:start -->
## Rust → Wasm

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64 · 2026-10-16. Medians of three 5-second trials.

### Server-rendered HTML (without in-memory page caching): 32 simultaneous clients

Each request authenticates a user, checks room membership, and renders the latest 50 messages. Database and operating-system caches remain active.

| Application runtime | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/request (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | **2,655 (1.00×)** | 113.3 (1.00×) | 426.8 (1.00×) | 114.9 (1.00×) | 17.28 (1.00×) |
| Rust Wasm / V8 + JS bindings | **2,545 (0.96×)** | 116.4 (1.03×) | 457.5 (1.07×) | 119.9 (1.04×) | 18.35 (1.06×) |
| Rust Wasm / V8 + direct host calls | **7,738 (2.91×)** | 127.0 (1.12×) | 164.3 (0.38×) | 62.5 (0.54×) | 6.00 (0.35×) |
| Rust Wasm / Wasmtime + direct host calls | **8,127 (3.06×)** | 127.0 (1.12×) | 156.3 (0.37×) | 52.1 (0.45×) | 5.76 (0.33×) |
| Native Rust | **11,093 (4.18×)** | 133.6 (1.18×) | 120.4 (0.28×) | 52.9 (0.46×) | 4.37 (0.25×) |

p99 response time: 99% of requests in a trial finished within this time.

### HTTP chat throughput (total requests/s): 32 simultaneous clients

| Request | JavaScript / V8 (baseline) | Rust Wasm / V8 + JS bindings | Rust Wasm / V8 + direct host calls | Rust Wasm / Wasmtime + direct host calls | Native Rust |
| --- | ---: | ---: | ---: | ---: | ---: |
| Read messages as JSON (no response cache) | 3,299 (1.00×) | 2,832 (0.86×) | 11,493 (3.48×) | 10,883 (3.30×) | 14,222 (4.31×) |
| Server-rendered HTML (with in-memory page caching) | 5,116 (1.00×) | 4,737 (0.93×) | 24,454 (4.78×) | 26,512 (5.18×) | 29,788 (5.82×) |
| Post a message; reply after saving it | 343 (1.00×) | 325 (0.95×) | 315 (0.92×) | 315 (0.92×) | 334 (0.97×) |

### WebSocket chat throughput (total messages/s)

Each room has one sender plus the receiving connections below. The sender waits for storage, broadcast, and every participant's HTTP read confirmation before sending the next message.

| Rooms and receiving connections | JavaScript / V8 (baseline) | Rust Wasm / V8 + JS bindings | Rust Wasm / V8 + direct host calls | Rust Wasm / Wasmtime + direct host calls | Native Rust |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 room, 32 receiving connections | 40 (1.00×) | 38 (0.96×) | 41 (1.04×) | 38 (0.94×) | 42 (1.05×) |
| 5 rooms, 8 receiving connections per room | 75 (1.00×) | 76 (1.02×) | 71 (0.95×) | 74 (0.99×) | 73 (0.97×) |

### Process startup and the Wasm compilation cache

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64. Medians of 10 fresh processes per row, starting from the same room data. The cached Wasmtime row loads machine code saved before timing. Filesystem caches stay warm.

| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 289.96 (1.00×) | 265.26 (1.00×) | 39.5 (1.00×) |
| Rust Wasm / V8 + JS bindings | 301.52 (1.04×) | 285.15 (1.07×) | 42.3 (1.07×) |
| Rust Wasm / V8 + direct host calls | 271.82 (0.94×) | 245.77 (0.93×) | 30.9 (0.78×) |
| Rust Wasm / Wasmtime: compile at startup | 324.56 (1.12×) | 326.18 (1.23×) | 32.1 (0.81×) |
| Rust Wasm / Wasmtime: reuse compiled module | 269.17 (0.93×) | 247.56 (0.93×) | 28.3 (0.71×) |
| Native Rust | 253.20 (0.87×) | 240.01 (0.90×) | 24.8 (0.63×) |

Wasm module compilation/loading: **66.21 ms → 1.03 ms** when reusing compiled code. All 10 cached trials hit the cache. Load tests exclude startup and compilation. [Raw data](results/2026-10-16/compile-cache/cold-cache-results.json).

[Complete report](results/2026-10-16/REPORT.md) · Raw data: [load tests](results/2026-10-16/results.json), [startup](results/2026-10-16/cold-results.json), [validation](results/2026-10-16/validation.json).
<!-- benchmark:end -->

## Request and binding paths

| Mode | Application code | SQL and WebSocket access |
| --- | --- | --- |
| `js` | JavaScript in V8 | JS Worker and Durable Object APIs |
| `wasm-js` | Rust Wasm in V8 | [JS Worker entry and JS bindings](application/wasm-worker.js) |
| `wasm-v8` | The same Wasm module in V8 | Rust callbacks through V8; no JS source execution |
| `wasm` | The same Wasm module in Wasmtime 49 | Rust host imports; no JS execution |
| `scriptc-wasm` | TypeScript compiled to Wasm in Wasmtime 49 | Rust host imports; no JS execution |
| `native` | The same Rust source compiled natively | Direct Rust calls |

```mermaid
flowchart LR
    REQUEST["Request"] --> HOST["celld Rust host"]
    HOST --> JS["js: JS Worker / room handlers (V8)"]
    JS --> JSAPI["JS bindings"]
    HOST --> SHIM["wasm-js: JS Worker / room wrappers (V8)"]
    SHIM --> WASMJS["Rust Wasm (V8)"] --> JSAPI
    HOST --> WASMV8["wasm-v8: Rust Wasm (V8)"] --> CALLBACKS["Rust callbacks through V8"]
    HOST --> WASMTIME["wasm: Rust Wasm (Wasmtime)"] --> IMPORTS["Rust host imports"]
    HOST --> SCRIPTC["scriptc-wasm: TypeScript Wasm (Wasmtime)"] --> IMPORTS
    HOST --> NATIVE["native: Rust handlers"]
    JSAPI --> STORE["celld SQLite / WebSocket"]
    CALLBACKS --> STORE
    IMPORTS --> STORE
    NATIVE --> STORE
```

`wasm-js` measures a standard Wasm Worker on celld: JS Worker → Rust Wasm → JS bindings. The three Rust Wasm modes use the same module. SQLite runs outside Wasm in every mode. These are local celld measurements; Cloudflare Workers is an architecture reference.

## Reproduce

[Build and run the Rust comparison](REPRODUCE.md) · [Build and run the scriptc comparison](experiments/scriptc/REPRODUCE.md).

## What the numbers measure

CPU and memory cover the entire celld server process, including runtime state, application caches, SQLite, and network buffers. Helper threads can make CPU use exceed one core.

The two V8 Wasm modes share the engine, module, business logic, and binary data format. Their difference includes the JS Worker entry, SQL cursor wrappers, conversion of SQL rows, and per-socket sends. These are measurements of the complete paths on celld.

The test uses one node and local persistence. Native Rust runs as trusted code. The direct Wasm prototype implements this application's HTTP, SQL, object calls, and WebSocket operations; tenant CPU enforcement and the complete asynchronous Workers API are outside its scope.

## Repository files

| Path | Contents |
| --- | --- |
| `application/` | JS chat implementation, JS-to-Wasm adapter, Worker configuration, and browser UI |
| `app/` | Rust chat logic, SQL, and database schema |
| `guest/` | Rust Wasm entry points and imports for host functions |
| `host.rs` | Rust host adapters for native Rust, Wasmtime, direct V8 Wasm, and scriptc |
| `load/` | HTTP and WebSocket load generators that check responses |
| `bench/` | Build, data preparation, measurement, and report scripts |
| `patches/celld.patch` | Integration with the pinned celld source and locked dependencies |
| `experiments/scriptc/` | TypeScript-to-Wasm chat application, Rust imports, results, and reproduction guide |
| `results/` | Recorded measurements, measured source files, and full reports |

## References

[Basecamp Campfire](https://github.com/basecamp/once-campfire/tree/edbc779f4dfc9b26c36310881711ddc976a9dfc8) supplies the chat-application reference. [Campfire in Rust](https://github.com/basecamp/once-campfire-rust/tree/6dae2fd3c1a2dce61ca11beb731bcce2b42b67ad) illustrates a native implementation and reproducible application benchmarks. Their published results use different applications, workloads, and hardware. The numbers here compare this celld application under the stated conditions.

Runtime base: [celld v0.6.2](https://github.com/denoland/celld/tree/90b43017241f81189453d326d05948f388b34652). License: [Apache-2.0](LICENSE); see [NOTICE](NOTICE) for retained upstream notices.

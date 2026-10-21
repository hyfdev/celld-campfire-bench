# workerd chat benchmark

Apple M4 Pro, 48 GiB RAM, macOS-26.5.2-arm64-arm-64bit-Mach-O · 2026-10-21. workerd v1.20261019.1 at `781048bc05cb205c92e503c1062ef3266ab959b5`. All three modes use the same patched release executable and the same seed: 50 Durable Objects with 1,000 messages each.

## Request and binding paths

| Mode | Request handler | SQL and WebSocket access |
| --- | --- | --- |
| `js` | Standard workerd JS Worker and DO handlers | Standard JS bindings |
| `wasm-js` | Standard JS Worker and DO entry → Rust Wasm in V8 | Wasm imports implemented in JS → standard workerd bindings |
| `wasm-v8` | Patched workerd native entry → the same Rust Wasm in V8 | C++ Wasm import callbacks → real workerd SQLite and WebSocket implementations |

The direct prototype keeps workerd's actor routing, activation, input gate, SQLite backend and automatic transactions, output gate, and WebSocket manager. Its startup module declares exports; its JS constructor, fetch handlers, and WebSocket handlers throw if invoked. V8's native function ABI and Wasm-to-host wrappers remain. No JS application or binding callback executes on the direct path.

This measures complete request paths. The direct path also removes JS Request/Response objects, SQL cursor and row conversion, and JS WebSocket wrappers. The difference is not an isolated timing of a single Wasm-to-JS transition. The prototype covers this application's API and synchronous Wasm handlers; full Workers API compatibility, tracing, cancellation, and tenant CPU limits are outside its scope.

## HTTP: 32 simultaneous clients

Medians of 3 5-second trials, with 2,000 warmup reads per trial. Each request authenticates a user and checks membership. Uncached reads query SQLite and return the latest 50 messages; cached HTML uses an application page cache. Database and operating-system caches remain active. Each client waits for a response before its next request.

### Server-rendered HTML (without in-memory page caching)

| Application runtime | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 4,289 (1.00×) | 101.5 (1.00×) | 236.7 (1.00×) | 177.0 (1.00×) | 12.26 (1.00×) |
| Rust Wasm / V8 + JS bindings | 3,921 (0.91×) | 101.7 (1.00×) | 259.6 (1.10×) | 205.2 (1.16×) | 16.96 (1.38×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 8,491 (1.98×) | 99.9 (0.98×) | 117.7 (0.50×) | 59.5 (0.34×) | 4.73 (0.39×) |

### Read messages as JSON (no response cache)

| Application runtime | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 5,702 (1.00×) | 101.5 (1.00×) | 178.1 (1.00×) | 188.3 (1.00×) | 8.87 (1.00×) |
| Rust Wasm / V8 + JS bindings | 4,490 (0.79×) | 102.3 (1.01×) | 227.8 (1.28×) | 208.3 (1.11×) | 13.38 (1.51×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 12,387 (2.17×) | 99.9 (0.98×) | 80.7 (0.45×) | 72.7 (0.39×) | 3.37 (0.38×) |

### Server-rendered HTML (with in-memory page caching)

| Application runtime | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 10,861 (1.00×) | 101.1 (1.00×) | 93.1 (1.00×) | 334.7 (1.00×) | 13.78 (1.00×) |
| Rust Wasm / V8 + JS bindings | 9,855 (0.91×) | 102.6 (1.01×) | 105.5 (1.13×) | 151.9 (0.45×) | 11.42 (0.83×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 26,577 (2.45×) | 99.8 (0.99×) | 37.6 (0.40×) | 127.5 (0.38×) | 1.72 (0.13×) |

### Post a message; reply after saving it

| Application runtime | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 6,291 (1.00×) | 91.9 (1.00×) | 145.6 (1.00×) | 233.9 (1.00×) | 27.21 (1.00×) |
| Rust Wasm / V8 + JS bindings | 5,926 (0.94×) | 93.4 (1.02×) | 157.5 (1.08×) | 225.9 (0.97×) | 24.24 (0.89×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 12,479 (1.98×) | 83.1 (0.90×) | 66.4 (0.46×) | 107.0 (0.46×) | 4.60 (0.17×) |

### Mixed reads and posts (100 posts/s target)

| Application runtime | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 10,204 (1.00×) | 101.1 (1.00×) | 99.0 (1.00×) | 244.5 (1.00×) | 19.63 (1.00×) |
| Rust Wasm / V8 + JS bindings | 8,902 (0.87×) | 104.8 (1.04×) | 114.4 (1.16×) | 172.4 (0.71×) | 12.59 (0.64×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 25,931 (2.54×) | 99.8 (0.99×) | 38.5 (0.39×) | 125.5 (0.51×) | 1.87 (0.10×) |

## WebSocket chat

Each room has one sender plus the receiving connections shown. The sender waits for storage, broadcast, and every participant's HTTP read confirmation before posting again. CPU time is per posted message; p99 is the completed broadcast-and-read-confirmation time.

### 1 room, 32 receiving connections

| Application runtime | Total messages/s | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 237 (1.00×) | 90.0 (1.00×) | 3,792.0 (1.00×) | 224.7 (1.00×) | 22.35 (1.00×) |
| Rust Wasm / V8 + JS bindings | 218 (0.92×) | 92.0 (1.02×) | 4,182.4 (1.10×) | 207.8 (0.92×) | 22.09 (0.99×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 415 (1.75×) | 80.9 (0.90×) | 1,929.9 (0.51×) | 150.6 (0.67×) | 3.72 (0.17×) |

### 5 rooms, 8 receiving connections per room

| Application runtime | Total messages/s | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 817 (1.00×) | 90.9 (1.00×) | 1,113.1 (1.00×) | 212.0 (1.00×) | 32.84 (1.00×) |
| Rust Wasm / V8 + JS bindings | 767 (0.94×) | 92.3 (1.02×) | 1,204.3 (1.08×) | 213.7 (1.01×) | 29.55 (0.90×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 1,457 (1.78×) | 82.4 (0.91×) | 565.9 (0.51×) | 151.7 (0.72×) | 5.57 (0.17×) |

## Fresh-process startup

Medians of 10 fresh processes per mode. Start → first room response includes process startup, the validated root page, and the first validated room page. Filesystem caches stay warm; no application compiled-module cache is configured.

| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 17.40 (1.00×) | 13.16 (1.00×) | 7.8 (1.00×) |
| Rust Wasm / V8 + JS bindings | 20.65 (1.19×) | 17.64 (1.34×) | 10.6 (1.36×) |
| Rust Wasm / V8 + direct C++ host calls (prototype) | 19.30 (1.11×) | 15.98 (1.21×) | 10.0 (1.28×) |

## Validation and measurement

All 45 HTTP pressure trials and 18 WebSocket pressure trials completed with zero client errors. Every trial ends with an audit of all 50 room databases. The dedicated functional suite checks 2,148 HTTP responses across the three modes, ordered broadcasts, cache invalidation, permissions, Unicode/NUL messages, and process-restart persistence. All 30 startup trials validate response bytes.

CPU and memory cover the whole workerd server process. CPU includes all its threads; memory is the macOS process footprint sampled every 100 ms during load. Ratios use the unrounded median for JS in the same workload. The load client runs on the same machine and its CPU is recorded separately. The three modes run sequentially with their order rotated between rounds.

These are single-node local workerd measurements. They are separate from the celld cohort: workerd's local SQLite persistence and celld's LTX persistence have different implementations. They do not measure Cloudflare's hosted network, distributed storage, or service limits.

[Load trials](results.json) · [Startup trials](cold-results.json) · [Functional validation](validation.json) · [Measured source snapshots](sources/) · [Reproduction instructions](../../experiments/workerd/README.md)

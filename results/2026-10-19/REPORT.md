# celld-campfire-bench: JavaScript, TypeScript Wasm, Rust Wasm, and native Rust

## The application and execution paths

The measured application is a Workers chat service with a stateless entry handler and one named Durable Object per room. It provides demo login, room membership, HTML and JSON history, posting, unread counts, page caching, and live WebSocket broadcasts. Each of 50 rooms starts with 1,000 messages. Reads authenticate a user, check membership, and select the latest 50 messages. Posting parses JSON and inserts a message; a SQLite trigger increments the room version counter and the other members' unread counts. Messages include Chinese, emoji, URLs, newlines, and HTML characters requiring escaping.

This is a small Campfire-style application implemented for this study. The native Rust reference is the same Rust business source compiled into celld. Celld itself is a native executable in every mode; the Wasm variants compile application code to Wasm. The runnable application and architecture diagram are in [the README](../../README.md).

All modes run in celld and share its request admission, named-object routing, room scheduling, SQLite cursors, WebSocket registry, and persistence system. Celld holds responses and broadcasts until storage confirms the writes they depend on. Persistence uses a local SQLite-backed object store on one node.

| Application runtime | Application code | SQL and WebSocket access |
| --- | --- | --- |
| JavaScript / V8 (baseline) | JavaScript in V8 (`v8` crate 152.1.0) | JS Worker and Durable Object APIs |
| Rust Wasm / V8 + JS bindings | Rust Wasm in V8 (`v8` crate 152.1.0) | JS Worker entry and JS bindings |
| Rust Wasm / V8 + direct host calls | Rust Wasm in V8 (`v8` crate 152.1.0) | Rust callbacks through V8; no JS source execution |
| Rust Wasm / Wasmtime + direct host calls | Rust Wasm in Wasmtime 49.0.0 | Wasm imports calling Rust directly |
| TypeScript → scriptc Wasm / Wasmtime | TypeScript compiled to Wasm with scriptc 0.3.6 in Wasmtime 49.0.0 | Wasm imports calling Rust directly |
| Native Rust | The same Rust source compiled natively | Direct Rust calls |

The **Wasmtime + direct host calls** mode handles requests and bindings in Rust and Wasm, with no JavaScript execution. The chat handlers compile to Wasm; SQLite runs outside Wasm. Requests, responses, and typed SQL rows use the `postcard` binary format. SQL transfer buffers are 512 KiB; Wasmtime limits linear memory to 128 MiB.

The **V8 + direct host calls** mode executes the same Wasm module. Its imports call Rust callbacks through V8's function/value interface, with no JS source execution.

The **V8 + JS bindings** mode imports the same `.wasm` module through celld's standard deployment loader. Its [`JS adapter`](../../application/wasm-worker.js) calls the Wasm entry points from JS Worker and Durable Object handlers; Wasm imports call `ctx.storage.sql.exec()`, SQL cursor wrappers, `getWebSockets()`, and per-socket `send()` in JS. SQL result strings are encoded by celld's JS TextEncoder before Wasm reads them; the direct paths transfer them as Rust/UTF-8. The V8 pair shares the engine, module, business logic, and binary data format.

The **scriptc Wasm** mode compiles TypeScript Worker and room handlers with scriptc 0.3.6, using its default release optimization and C runtime. Wasmtime runs the application with Rust imports and no JS execution. Requests, actions, SQL parameters, and SQL rows cross the TS/Rust boundary as JSON; SQL response buffers are 4 MiB and linear memory is limited to 128 MiB. It uses the shared SQL, schema, data, and response-cache policy.

These are complete application measurements. JS bindings use cursor wrappers and per-socket sends. Direct Rust bindings transfer typed SQL rows and provide a host broadcast operation. The results include request handling, binding APIs, data conversion, runtime bookkeeping, and generated machine code.

## Measurement method

Tests run on Apple M4 Pro with 14 logical CPUs, 48 GiB RAM, and macOS 26.5.2 arm64. The celld base is commit `90b43017241f81189453d326d05948f388b34652`. The compiler is `rustc 1.96.0 (ac68faa20 2026-05-25)`. The host/native release build uses optimization level 3, thin LTO, and 16 codegen units. The Rust Wasm build uses optimization level 3, full LTO, panic abort, SIMD128, and bulk-memory instructions. Wasmtime uses Cranelift's speed optimization and compiles its module once per process. Native Rust uses the compiler's default host CPU target.

The request executor uses one Tokio thread. The Worker entry has one runtime instance, and each room runtime instance holds up to 32 Durable Objects. Storage, object ownership, and engine helper threads remain active, so the process can use more than one CPU core. Each object allows up to 256 requests in flight; the HTTP load tests use at most 33 across the server. Every load trial starts a fresh process from the same initial room data.

HTTP tests use 32 simultaneous clients for 5 seconds, then wait for outstanding responses. Each client waits for its complete response before sending another request. Tests use 3 rounds. The 6 runtimes run one at a time; their order rotates between rounds. Warmup checks 2,000 reads across every room, four users, and both formats. Posting and WebSocket cases also warm 1,000 writes, 20 per room. The HTML/JSON tests labeled without response caching generate each response from the database; database and operating-system caches remain active. Cached reads still authenticate, check membership, and read the room version: three application SQL calls; a miss adds the message query. The cache limit is 1 MiB per room and all writes invalidate its version.

| Request | Server work |
| --- | --- |
| Server-rendered HTML (without in-memory page caching) | Select and render the latest 50 messages for every request |
| Read messages as JSON (no response cache) | Select and serialize the latest 50 messages for every request |
| Server-rendered HTML (with in-memory page caching) | Authenticate and check the version, then return cached page bytes |
| Post a message; reply after saving it | Authenticate, parse JSON, insert, update version/unread counts, wait for storage confirmation |
| Read message history while posting new messages | Reads with response caching: 80% HTML / 20% JSON, plus one posting client attempting 100 posts/s |

The posting client waits for each response and skips missed send intervals. A slower server can therefore complete fewer posts and have a different response-cache hit rate. The read-only tests compare equal work per request; the mixed test reports the achieved read/write rates separately.

WebSocket tests use one sending connection per room, plus either 32 receiving connections in one room or eight in each of five rooms. Senders also receive their own broadcasts, giving 33 or 45 total connections. Senders use user 1; receiving connections share user 2. These counts describe connections, not distinct users.

Each connection marks the room read when joining and after every delivered message, using an HTTP request. That request authenticates the user, checks membership, clears the unread count, increments the room version, and waits for storage confirmation. Joining finishes before timing. Each sender waits for its broadcast and every participant's read confirmation before sending the next message. The browser UI can overlap confirmations across messages; the benchmark waits between messages.

Receiving clients verify message text, IDs, timestamps, and room versions. Read confirmations must have the complete expected sequence of unique versions. The table distinguishes new chat messages from deliveries to individual connections. Sender latency ends when the sender receives its message; complete-message latency ends after all read confirmations. CPU time/message includes broadcasts and HTTP read confirmations. Clients wait a final 200 ms for any extra deliveries before checking the counts.

CPU is user plus system time of the server process from `proc_pid_rusage`, with Mach ticks converted using the recorded timebase of 125/3. CPU time/request or CPU time/message divides that time by the successful request or new-message count. CPU percentage uses one full core as 100%; helper threads can make it exceed 100%. Faster implementations can have a larger total CPU percentage because they finish more work. CPU time/request or CPU time/message shows the CPU used for the same unit of work. Load-client CPU is recorded separately.

Memory is the whole server process, including engine state, application caches, SQLite, and HTTP/WebSocket buffers. The primary memory number is the maximum sampled physical footprint, sampled every 100 ms and at both endpoints. RSS is also recorded. Load-test tables report the median of 3 trial maxima, in MiB (1,048,576 bytes). Read-only trials retain identical data; write trials add different numbers of rows at different achieved throughputs.

HTTP response time runs from sending a request to receiving its complete body, including time spent waiting behind other requests. p99 means 99% of measured requests finished within that time. The results show capacity with the stated number of clients; those clients wait for responses, so they reduce their sending rate when the server slows down. Tables report medians of trial values, with minimum–maximum throughput ranges.

## Results

### Server-rendered HTML (without in-memory page caching)

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 2,594 (1.00×)<br>2,472–2,603 | 437.87 (1.00×) | 113.5 (1.00×) | 117.0 (1.00×) | 18.55 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 2,367 (0.91×)<br>2,346–2,455 | 499.17 (1.14×) | 117.4 (1.03×) | 119.6 (1.02×) | 20.72 (1.12×) |
| 32 | Rust Wasm / V8 + direct host calls | 7,625 (2.94×)<br>7,541–7,813 | 169.83 (0.39×) | 129.7 (1.14×) | 65.3 (0.56×) | 5.96 (0.32×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 7,918 (3.05×)<br>7,740–8,147 | 164.36 (0.38×) | 130.7 (1.15×) | 52.0 (0.44×) | 5.73 (0.31×) |
| 32 | TypeScript → scriptc Wasm / Wasmtime | 1,594 (0.61×)<br>1,578–1,658 | 654.05 (1.49×) | 104.7 (0.92×) | 65.5 (0.56×) | 38.48 (2.07×) |
| 32 | Native Rust | 10,727 (4.14×)<br>10,682–11,006 | 129.09 (0.29×) | 138.6 (1.22×) | 45.1 (0.39×) | 4.44 (0.24×) |

### Read messages as JSON (no response cache)

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 3,105 (1.00×)<br>3,002–3,212 | 372.99 (1.00×) | 115.5 (1.00×) | 122.1 (1.00×) | 14.95 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 2,553 (0.82×)<br>2,364–2,728 | 462.02 (1.24×) | 117.9 (1.02×) | 123.0 (1.01×) | 26.58 (1.78×) |
| 32 | Rust Wasm / V8 + direct host calls | 10,693 (3.44×)<br>10,471–10,990 | 131.08 (0.35×) | 139.1 (1.20×) | 65.9 (0.54×) | 4.55 (0.30×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 10,440 (3.36×)<br>10,379–10,451 | 132.11 (0.35×) | 138.0 (1.19×) | 52.3 (0.43×) | 4.60 (0.31×) |
| 32 | TypeScript → scriptc Wasm / Wasmtime | 5,161 (1.66×)<br>4,920–5,481 | 232.50 (0.62×) | 120.0 (1.04×) | 68.4 (0.56×) | 11.06 (0.74×) |
| 32 | Native Rust | 13,710 (4.42×)<br>13,648–14,081 | 108.38 (0.29×) | 148.6 (1.29×) | 52.8 (0.43×) | 3.52 (0.24×) |

### Server-rendered HTML (with in-memory page caching)

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 4,878 (1.00×)<br>4,835–4,921 | 255.98 (1.00×) | 124.6 (1.00×) | 132.4 (1.00×) | 8.99 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 4,562 (0.94×)<br>4,392–4,749 | 274.05 (1.07×) | 125.0 (1.00×) | 136.9 (1.03×) | 9.91 (1.10×) |
| 32 | Rust Wasm / V8 + direct host calls | 22,599 (4.63×)<br>22,575–22,904 | 78.20 (0.31×) | 177.0 (1.42×) | 77.9 (0.59×) | 2.28 (0.25×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 23,031 (4.72×)<br>22,341–23,756 | 76.29 (0.30×) | 175.7 (1.41×) | 67.7 (0.51×) | 2.63 (0.29×) |
| 32 | TypeScript → scriptc Wasm / Wasmtime | 14,470 (2.97×)<br>14,450–14,490 | 106.47 (0.42×) | 154.1 (1.24×) | 97.8 (0.74×) | 3.41 (0.38×) |
| 32 | Native Rust | 26,337 (5.40×)<br>26,244–26,614 | 68.39 (0.27×) | 181.0 (1.45×) | 61.3 (0.46×) | 2.00 (0.22×) |

### Post a message; reply after saving it

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 443 (1.00×)<br>369–497 | 1396.03 (1.00×) | 68.2 (1.00×) | 116.5 (1.00×) | 670.19 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 448 (1.01×)<br>419–477 | 1421.80 (1.02×) | 67.6 (0.99×) | 122.7 (1.05×) | 628.08 (0.94×) |
| 32 | Rust Wasm / V8 + direct host calls | 411 (0.93×)<br>398–420 | 1417.23 (1.02×) | 59.5 (0.87×) | 76.8 (0.66×) | 724.10 (1.08×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 427 (0.96×)<br>370–449 | 1325.07 (0.95×) | 56.6 (0.83×) | 60.5 (0.52×) | 804.37 (1.20×) |
| 32 | TypeScript → scriptc Wasm / Wasmtime | 410 (0.92×)<br>380–411 | 1280.52 (0.92×) | 50.7 (0.74×) | 82.6 (0.71×) | 720.76 (1.08×) |
| 32 | Native Rust | 443 (1.00×)<br>405–493 | 1377.54 (0.99×) | 60.1 (0.88×) | 56.4 (0.48×) | 549.64 (0.82×) |

### Read message history while posting new messages

One additional client attempts 100 posts/s. Requests/s and CPU time/request include both reads and posts.

| Reading clients (+1 posting client) | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 4,536 (1.00×)<br>4,143–4,774 | 292.57 (1.00×) | 133.6 (1.00×) | 153.9 (1.00×) | 14.39 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 3,967 (0.87×)<br>3,829–4,329 | 342.84 (1.17×) | 136.0 (1.02×) | 161.8 (1.05×) | 16.34 (1.14×) |
| 32 | Rust Wasm / V8 + direct host calls | 20,148 (4.44×)<br>19,580–20,332 | 91.50 (0.31×) | 179.2 (1.34×) | 101.9 (0.66×) | 8.08 (0.56×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 22,957 (5.06×)<br>21,322–23,422 | 82.12 (0.28×) | 187.6 (1.40×) | 87.5 (0.57×) | 7.31 (0.51×) |
| 32 | TypeScript → scriptc Wasm / Wasmtime | 11,437 (2.52×)<br>9,344–12,075 | 129.97 (0.44×) | 148.7 (1.11×) | 144.4 (0.94×) | 8.78 (0.61×) |
| 32 | Native Rust | 25,641 (5.65×)<br>22,543–27,638 | 74.60 (0.25×) | 191.3 (1.43×) | 82.5 (0.54×) | 7.08 (0.49×) |

| Reading clients (+1 posting client) | Application runtime | Reads/s | Posts/s | Reads served from response cache (%) |
| ---: | --- | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 4,467 (1.00×) | 69.2 (1.00×) | 93.9 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 3,905 (0.87×) | 61.9 (0.89×) | 93.8 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 20,049 (4.49×) | 99.1 (1.43×) | 98.0 (1.04×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 22,857 (5.12×) | 99.7 (1.44×) | 98.3 (1.05×) |
| 32 | TypeScript → scriptc Wasm / Wasmtime | 11,338 (2.54×) | 98.9 (1.43×) | 96.5 (1.03×) |
| 32 | Native Rust | 25,541 (5.72×) | 99.7 (1.44×) | 98.4 (1.05×) |

### WebSocket chat throughput and server resources

| Rooms and receiving connections | Application runtime | Total messages/s; min–max across trials | CPU time/message (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 room, 32 receiving connections | JavaScript / V8 (baseline) | 39 (1.00×)<br>39–43 | 40998.38 (1.00×) | 172.1 (1.00×) | 125.2 (1.00×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + JS bindings | 39 (1.00×)<br>38–41 | 42875.50 (1.05×) | 172.6 (1.00×) | 131.2 (1.05×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + direct host calls | 43 (1.11×)<br>42–46 | 42100.31 (1.03×) | 178.3 (1.04×) | 81.7 (0.65×) |
| 1 room, 32 receiving connections | Rust Wasm / Wasmtime + direct host calls | 43 (1.10×)<br>42–47 | 41309.23 (1.01×) | 178.0 (1.03×) | 68.1 (0.54×) |
| 1 room, 32 receiving connections | TypeScript → scriptc Wasm / Wasmtime | 44 (1.14×)<br>42–46 | 38274.62 (0.93×) | 169.7 (0.99×) | 92.5 (0.74×) |
| 1 room, 32 receiving connections | Native Rust | 43 (1.12×)<br>43–46 | 39328.71 (0.96×) | 178.2 (1.04×) | 67.7 (0.54×) |
| 5 rooms, 8 receiving connections per room | JavaScript / V8 (baseline) | 89 (1.00×)<br>83–92 | 12226.36 (1.00×) | 109.3 (1.00×) | 118.0 (1.00×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + JS bindings | 87 (0.97×)<br>86–90 | 12841.67 (1.05×) | 115.3 (1.05×) | 128.3 (1.09×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + direct host calls | 88 (0.99×)<br>81–89 | 10848.74 (0.89×) | 96.4 (0.88×) | 79.2 (0.67×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / Wasmtime + direct host calls | 80 (0.90×)<br>79–87 | 11014.92 (0.90×) | 96.0 (0.88×) | 65.3 (0.55×) |
| 5 rooms, 8 receiving connections per room | TypeScript → scriptc Wasm / Wasmtime | 86 (0.97×)<br>77–87 | 10772.59 (0.88×) | 91.9 (0.84×) | 83.3 (0.71×) |
| 5 rooms, 8 receiving connections per room | Native Rust | 84 (0.95×)<br>79–89 | 11177.90 (0.91×) | 94.3 (0.86×) | 60.5 (0.51×) |

### WebSocket deliveries and read confirmations

| Rooms and receiving connections | Application runtime | Message deliveries/s | HTTP read confirmations/s | Send → sender receives message: p99 (ms) | Send → all read confirmations: p99 (ms) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 room, 32 receiving connections | JavaScript / V8 (baseline) | 1,284 (1.00×) | 1,284 (1.00×) | 14.01 (1.00×) | 34.38 (1.00×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + JS bindings | 1,286 (1.00×) | 1,286 (1.00×) | 14.17 (1.01×) | 36.98 (1.08×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + direct host calls | 1,422 (1.11×) | 1,422 (1.11×) | 14.82 (1.06×) | 32.65 (0.95×) |
| 1 room, 32 receiving connections | Rust Wasm / Wasmtime + direct host calls | 1,417 (1.10×) | 1,417 (1.10×) | 14.00 (1.00×) | 33.23 (0.97×) |
| 1 room, 32 receiving connections | TypeScript → scriptc Wasm / Wasmtime | 1,463 (1.14×) | 1,463 (1.14×) | 14.31 (1.02×) | 32.66 (0.95×) |
| 1 room, 32 receiving connections | Native Rust | 1,434 (1.12×) | 1,434 (1.12×) | 13.90 (0.99×) | 30.95 (0.90×) |
| 5 rooms, 8 receiving connections per room | JavaScript / V8 (baseline) | 803 (1.00×) | 803 (1.00×) | 93.80 (1.00×) | 146.65 (1.00×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + JS bindings | 780 (0.97×) | 780 (0.97×) | 80.89 (0.86×) | 161.21 (1.10×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + direct host calls | 795 (0.99×) | 795 (0.99×) | 115.14 (1.23×) | 202.50 (1.38×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / Wasmtime + direct host calls | 724 (0.90×) | 724 (0.90×) | 122.91 (1.31×) | 263.89 (1.80×) |
| 5 rooms, 8 receiving connections per room | TypeScript → scriptc Wasm / Wasmtime | 775 (0.97×) | 775 (0.97×) | 122.75 (1.31×) | 191.95 (1.31×) |
| 5 rooms, 8 receiving connections per room | Native Rust | 759 (0.95×) | 759 (0.95×) | 95.10 (1.01×) | 256.91 (1.75×) |


## Process startup

### Compile Wasm in each new process

Each runtime starts in 10 fresh processes from the same initial room data. The runner polls for a validated home page every 1 ms, then requests the first room's HTML page. Filesystem caches stay warm; compiled Wasm code is shared only within each process. The room response includes object activation, storage opening, and application initialization. Columns report medians independently, so the median parts need not add up to the median total. Memory is sampled after the first room response.

| Application runtime | Start → home page response (ms) | Home page → first room response (ms) | Start → first room response (ms); min–max across trials | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 258.40 (1.00×) | 31.23 (1.00×) | 288.73 (1.00×)<br>279.60–319.55 | 39.2 (1.00×) |
| Rust Wasm / V8 + JS bindings | 265.87 (1.03×) | 36.15 (1.16×) | 303.14 (1.05×)<br>290.35–312.11 | 41.4 (1.05×) |
| Rust Wasm / V8 + direct host calls | 251.64 (0.97×) | 24.48 (0.78×) | 278.08 (0.96×)<br>261.63–314.50 | 30.7 (0.78×) |
| Rust Wasm / Wasmtime + direct host calls | 310.60 (1.20×) | 23.36 (0.75×) | 333.93 (1.16×)<br>327.70–381.79 | 31.5 (0.80×) |
| TypeScript → scriptc Wasm / Wasmtime | 412.62 (1.60×) | 24.21 (0.78×) | 436.62 (1.51×)<br>429.00–457.58 | 42.9 (1.09×) |
| Native Rust | 242.27 (0.94×) | 23.40 (0.75×) | 265.71 (0.92×)<br>253.72–284.53 | 24.6 (0.63×) |

### Process startup and the Wasm compilation cache

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64. Medians of 10 fresh processes per row, starting from the same room data. The cached Wasmtime row loads machine code saved before timing. Filesystem caches stay warm.

| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 290.72 (1.00×) | 271.70 (1.00×) | 39.4 (1.00×) |
| Rust Wasm / V8 + JS bindings | 308.10 (1.06×) | 287.06 (1.06×) | 42.2 (1.07×) |
| Rust Wasm / V8 + direct host calls | 268.21 (0.92×) | 244.77 (0.90×) | 31.0 (0.79×) |
| Rust Wasm / Wasmtime: compile at startup | 334.06 (1.15×) | 306.42 (1.13×) | 31.2 (0.79×) |
| Rust Wasm / Wasmtime: reuse compiled module | 269.19 (0.93×) | 241.32 (0.89×) | 28.1 (0.71×) |
| TypeScript → scriptc Wasm / Wasmtime: compile at startup | 442.62 (1.52×) | 406.71 (1.50×) | 42.9 (1.09×) |
| Native Rust | 270.50 (0.93×) | 240.40 (0.88×) | 25.1 (0.64×) |

Wasm module compilation/loading: **69.14 ms → 1.05 ms** when reusing compiled code. All 10 cached trials hit the cache. Load tests exclude startup and compilation. [Raw data](compile-cache/cold-cache-results.json).

## Correctness checks and recorded sources

The `results.json`, `cold-results.json`, and `validation.json` records contain **90 HTTP trials, 36 WebSocket trials, and 60 startup trials**, plus **4,296 dedicated full-response checks**. The HTTP load clients completed 3,973,375 requests, including 46,790 posts. The WebSocket sessions added 11,568 posts, 196,248 verified message deliveries, and 196,248 verified HTTP read confirmations. All trials passed response and ordering checks; all 126 post-trial audits passed across all 50 room databases. Source hashes agree across these three records, and captured files match their recorded SHA-256 hashes.

The `compile-cache/` comparison contains **70 additional startup trials**. Its referenced source files match their recorded SHA-256 hashes.

The load tests started at 2026-10-19T08:54:38Z and completed at 2026-10-19T09:17:10Z. The load-test JSON records the before/after thermal checks. The largest recorded load-client CPU usage was 72.9% of one core; clients ran in separate processes on the same machine.

An independent Python implementation produces expected response bytes from the initial data and confirmed posts. Dedicated validation and warmup compare complete bodies. The HTTP load client checks every read's full-body length and CRC32, verifies write acknowledgments, and requires unique write versions with no gaps. A read must include any write confirmed before that read was sent. After each load trial the runner checks every room's current page and audits every SQLite message, timestamp, user, unread count, room version, and `PRAGMA integrity_check`.

Dedicated checks cover invalid credentials, malformed login types and membership, cache invalidation, multiple users, 50 maximum-length messages, HTML expanding beyond 1 MiB, and 8 KiB messages whose JSON encoding exceeds 32 KiB. Restart validation deletes celld's entire local runtime directory and recovers all rooms from the object store, retaining the checked messages and unread changes.

The full data are [load tests and resource use](results.json), [startup](cold-results.json), and [correctness](validation.json). They contain SHA-256 hashes of the measured binaries, guest, application, adapters, clients, and relevant sources and lockfiles. The `source_snapshot` field identifies the captured source files. The `recorded_only` map identifies hashes whose corresponding files are not distributed with the data. Build, test, measurement, and report scripts live under `bench/`. Run phases and build commands are in the [reproduction guide](../../REPRODUCE.md).

## What this benchmark covers

The direct adapters implement this application's buffered HTTP, SQL, object routing, and WebSocket needs. They are not a general implementation of every Workers API. Native Rust is a trusted-code performance reference and does not provide an untrusted-code sandbox. The Wasm adapters enforce memory ranges and object storage scope, but these runs do not include production tenant CPU interruption or prove hostile-tenant isolation. This is a single-node, local persistence study; it does not measure a fleet, remote-bucket latency, network failure, or global placement.

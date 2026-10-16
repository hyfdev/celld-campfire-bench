# celld-campfire-bench: JavaScript, Rust Wasm, and native Rust

## The application and execution paths

The measured application is a Workers chat service with a stateless entry handler and one named Durable Object per room. It provides demo login, room membership, HTML and JSON history, posting, unread counts, page caching, and live WebSocket broadcasts. Each of 50 rooms starts with 1,000 messages. Reads authenticate a user, check membership, and select the latest 50 messages. Posting parses JSON and inserts a message; a SQLite trigger increments the room version counter and the other members' unread counts. Messages include Chinese, emoji, URLs, newlines, and HTML characters requiring escaping.

This is a small Campfire-style application implemented for this study. The native Rust reference is the same Rust business source compiled into celld. Celld itself is a native executable in every mode; only the Rust application's Wasm variants are compiled to Wasm. The runnable application and architecture diagram are in [the README](../../README.md).

All modes run in celld and share its request admission, named-object routing, room scheduling, SQLite cursors, WebSocket registry, and persistence system. Celld holds responses and broadcasts until storage confirms the writes they depend on. Persistence uses a local SQLite-backed object store on one node.

| Application runtime | Application code | SQL and WebSocket access |
| --- | --- | --- |
| JavaScript / V8 (baseline) | JavaScript in V8 (`v8` crate 152.1.0) | JS Worker and Durable Object APIs |
| Rust Wasm / V8 + JS bindings | Rust Wasm in V8 (`v8` crate 152.1.0) | JS Worker entry and JS bindings |
| Rust Wasm / V8 + direct host calls | Rust Wasm in V8 (`v8` crate 152.1.0) | Rust callbacks through V8; no JS source execution |
| Rust Wasm / Wasmtime + direct host calls | Rust Wasm in Wasmtime 49.0.0 | Wasm imports calling Rust directly |
| Native Rust | The same Rust source compiled natively | Direct Rust calls |

The **Wasmtime + direct host calls** mode handles requests and bindings in Rust and Wasm, with no JavaScript execution. The chat handlers compile to Wasm; SQLite runs outside Wasm. Requests, responses, and typed SQL rows use the `postcard` binary format. SQL transfer buffers are 512 KiB; Wasmtime limits linear memory to 128 MiB.

The **V8 + direct host calls** mode executes the same Wasm module. Its imports call Rust callbacks through V8's function/value interface, with no JS source execution.

The **V8 + JS bindings** mode imports the same `.wasm` module through celld's standard deployment loader. Its [`JS adapter`](../../application/wasm-worker.js) calls the Wasm entry points from JS Worker and Durable Object handlers; Wasm imports call `ctx.storage.sql.exec()`, SQL cursor wrappers, `getWebSockets()`, and per-socket `send()` in JS. SQL result strings are encoded by celld's JS TextEncoder before Wasm reads them; the direct paths transfer them as Rust/UTF-8. The V8 pair shares the engine, module, business logic, and binary data format.

These are complete application measurements. JS bindings use cursor wrappers and per-socket sends. Direct Rust bindings transfer typed SQL rows and provide a host broadcast operation. The results include request handling, binding APIs, data conversion, runtime bookkeeping, and generated machine code.

## Measurement method

Tests run on Apple M4 Pro with 14 logical CPUs, 48 GiB RAM, and macOS 26.5.2 arm64. The celld base is commit `90b43017241f81189453d326d05948f388b34652`. The compiler is `rustc 1.96.0 (ac68faa20 2026-05-25)`. The host/native release build uses optimization level 3, thin LTO, and 16 codegen units. The Wasm build uses optimization level 3, full LTO, panic abort, SIMD128, and bulk-memory instructions. Wasmtime uses Cranelift's speed optimization and compiles its module once per process. Native Rust uses the compiler's default host CPU target.

The request executor uses one Tokio thread. The Worker entry has one runtime instance, and each room runtime instance holds up to 32 Durable Objects. Storage, object ownership, and engine helper threads remain active, so the process can use more than one CPU core. Each object allows up to 256 requests in flight; the HTTP load tests use at most 33 across the server. Every load trial starts a fresh process from the same initial room data.

HTTP tests use 32 simultaneous clients for 5 seconds, then wait for outstanding responses. Each client waits for its complete response before sending another request. Tests use 3 rounds. The 5 runtimes run one at a time; their order rotates between rounds. Warmup checks 2,000 reads across every room, four users, and both formats. Posting and WebSocket cases also warm 1,000 writes, 20 per room. The HTML/JSON tests labeled without response caching generate each response from the database; database and operating-system caches remain active. Cached reads still authenticate, check membership, and read the room version: three application SQL calls; a miss adds the message query. The cache limit is 1 MiB per room and all writes invalidate its version.

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
| 32 | JavaScript / V8 (baseline) | 2,655 (1.00×)<br>2,602–2,667 | 426.79 (1.00×) | 113.3 (1.00×) | 114.9 (1.00×) | 17.28 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 2,545 (0.96×)<br>2,454–2,567 | 457.48 (1.07×) | 116.4 (1.03×) | 119.9 (1.04×) | 18.35 (1.06×) |
| 32 | Rust Wasm / V8 + direct host calls | 7,738 (2.91×)<br>7,724–7,895 | 164.29 (0.38×) | 127.0 (1.12×) | 62.5 (0.54×) | 6.00 (0.35×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 8,127 (3.06×)<br>8,090–8,184 | 156.25 (0.37×) | 127.0 (1.12×) | 52.1 (0.45×) | 5.76 (0.33×) |
| 32 | Native Rust | 11,093 (4.18×)<br>10,615–11,474 | 120.42 (0.28×) | 133.6 (1.18×) | 52.9 (0.46×) | 4.37 (0.25×) |

### Read messages as JSON (no response cache)

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 3,299 (1.00×)<br>3,237–3,312 | 348.10 (1.00×) | 114.8 (1.00×) | 124.1 (1.00×) | 13.62 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 2,832 (0.86×)<br>2,802–2,869 | 416.96 (1.20×) | 118.1 (1.03×) | 122.7 (0.99×) | 16.66 (1.22×) |
| 32 | Rust Wasm / V8 + direct host calls | 11,493 (3.48×)<br>11,085–11,512 | 117.76 (0.34×) | 135.4 (1.18×) | 71.5 (0.58×) | 4.24 (0.31×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 10,883 (3.30×)<br>10,719–10,936 | 123.01 (0.35×) | 133.9 (1.17×) | 52.3 (0.42×) | 4.42 (0.32×) |
| 32 | Native Rust | 14,222 (4.31×)<br>14,119–14,376 | 100.64 (0.29×) | 143.1 (1.25×) | 51.4 (0.41×) | 3.60 (0.26×) |

### Server-rendered HTML (with in-memory page caching)

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 5,116 (1.00×)<br>5,039–5,134 | 241.39 (1.00×) | 123.8 (1.00×) | 133.1 (1.00×) | 8.69 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 4,737 (0.93×)<br>4,692–4,880 | 263.96 (1.09×) | 123.9 (1.00×) | 137.7 (1.03×) | 10.21 (1.18×) |
| 32 | Rust Wasm / V8 + direct host calls | 24,454 (4.78×)<br>23,678–24,648 | 72.54 (0.30×) | 176.6 (1.43×) | 78.1 (0.59×) | 2.43 (0.28×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 26,512 (5.18×)<br>24,676–26,581 | 67.74 (0.28×) | 179.0 (1.45×) | 67.8 (0.51×) | 2.04 (0.24×) |
| 32 | Native Rust | 29,788 (5.82×)<br>29,078–31,357 | 61.42 (0.25×) | 183.0 (1.48×) | 62.5 (0.47×) | 1.72 (0.20×) |

### Post a message; reply after saving it

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 343 (1.00×)<br>310–370 | 2287.90 (1.00×) | 74.9 (1.00×) | 112.8 (1.00×) | 718.10 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 325 (0.95×)<br>306–345 | 2473.11 (1.08×) | 80.5 (1.07×) | 119.7 (1.06×) | 821.57 (1.14×) |
| 32 | Rust Wasm / V8 + direct host calls | 315 (0.92×)<br>278–387 | 2410.88 (1.05×) | 75.3 (1.00×) | 79.9 (0.71×) | 870.87 (1.21×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 315 (0.92×)<br>285–330 | 2263.43 (0.99×) | 69.5 (0.93×) | 64.2 (0.57×) | 917.90 (1.28×) |
| 32 | Native Rust | 334 (0.97×)<br>299–334 | 2347.33 (1.03×) | 73.3 (0.98×) | 59.1 (0.52×) | 828.74 (1.15×) |

### Read message history while posting new messages

One additional client attempts 100 posts/s. Requests/s and CPU time/request include both reads and posts.

| Reading clients (+1 posting client) | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 4,598 (1.00×)<br>4,567–4,769 | 287.40 (1.00×) | 133.7 (1.00×) | 153.0 (1.00×) | 14.56 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 4,419 (0.96×)<br>4,397–4,448 | 301.81 (1.05×) | 134.1 (1.00×) | 165.1 (1.08×) | 15.02 (1.03×) |
| 32 | Rust Wasm / V8 + direct host calls | 23,484 (5.11×)<br>23,413–23,737 | 78.28 (0.27×) | 185.8 (1.39×) | 101.4 (0.66×) | 7.37 (0.51×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 23,825 (5.18×)<br>21,919–23,955 | 77.60 (0.27×) | 184.6 (1.38×) | 86.5 (0.57×) | 7.61 (0.52×) |
| 32 | Native Rust | 27,957 (6.08×)<br>26,819–28,520 | 69.82 (0.24×) | 192.8 (1.44×) | 82.8 (0.54×) | 6.95 (0.48×) |

| Reading clients (+1 posting client) | Application runtime | Reads/s | Posts/s | Reads served from response cache (%) |
| ---: | --- | ---: | ---: | ---: |
| 32 | JavaScript / V8 (baseline) | 4,531 (1.00×) | 68.1 (1.00×) | 94.2 (1.00×) |
| 32 | Rust Wasm / V8 + JS bindings | 4,353 (0.96×) | 67.1 (0.99×) | 93.9 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 23,385 (5.16×) | 99.5 (1.46×) | 98.3 (1.04×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 23,727 (5.24×) | 99.5 (1.46×) | 98.3 (1.04×) |
| 32 | Native Rust | 27,857 (6.15×) | 99.7 (1.46×) | 98.6 (1.05×) |

### WebSocket chat throughput and server resources

| Rooms and receiving connections | Application runtime | Total messages/s; min–max across trials | CPU time/message (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 room, 32 receiving connections | JavaScript / V8 (baseline) | 40 (1.00×)<br>36–40 | 46270.21 (1.00×) | 175.2 (1.00×) | 124.5 (1.00×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + JS bindings | 38 (0.96×)<br>35–39 | 45349.48 (0.98×) | 173.6 (0.99×) | 129.6 (1.04×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + direct host calls | 41 (1.04×)<br>35–42 | 43752.81 (0.95×) | 181.0 (1.03×) | 81.6 (0.66×) |
| 1 room, 32 receiving connections | Rust Wasm / Wasmtime + direct host calls | 38 (0.94×)<br>37–41 | 44660.81 (0.97×) | 167.9 (0.96×) | 68.9 (0.55×) |
| 1 room, 32 receiving connections | Native Rust | 42 (1.05×)<br>37–43 | 42691.61 (0.92×) | 177.7 (1.01×) | 63.2 (0.51×) |
| 5 rooms, 8 receiving connections per room | JavaScript / V8 (baseline) | 75 (1.00×)<br>61–76 | 17001.74 (1.00×) | 116.6 (1.00×) | 113.2 (1.00×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + JS bindings | 76 (1.02×)<br>63–77 | 16914.68 (0.99×) | 117.7 (1.01×) | 126.9 (1.12×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + direct host calls | 71 (0.95×)<br>64–74 | 16521.92 (0.97×) | 110.1 (0.94×) | 78.0 (0.69×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / Wasmtime + direct host calls | 74 (0.99×)<br>65–76 | 14716.24 (0.87×) | 101.2 (0.87×) | 63.6 (0.56×) |
| 5 rooms, 8 receiving connections per room | Native Rust | 73 (0.97×)<br>59–77 | 14566.65 (0.86×) | 95.8 (0.82×) | 56.9 (0.50×) |

### WebSocket deliveries and read confirmations

| Rooms and receiving connections | Application runtime | Message deliveries/s | HTTP read confirmations/s | Send → sender receives message: p99 (ms) | Send → all read confirmations: p99 (ms) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 room, 32 receiving connections | JavaScript / V8 (baseline) | 1,315 (1.00×) | 1,315 (1.00×) | 14.89 (1.00×) | 35.97 (1.00×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + JS bindings | 1,265 (0.96×) | 1,265 (0.96×) | 16.25 (1.09×) | 41.69 (1.16×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + direct host calls | 1,365 (1.04×) | 1,365 (1.04×) | 17.35 (1.17×) | 36.47 (1.01×) |
| 1 room, 32 receiving connections | Rust Wasm / Wasmtime + direct host calls | 1,238 (0.94×) | 1,238 (0.94×) | 19.42 (1.30×) | 55.93 (1.55×) |
| 1 room, 32 receiving connections | Native Rust | 1,384 (1.05×) | 1,384 (1.05×) | 14.56 (0.98×) | 35.89 (1.00×) |
| 5 rooms, 8 receiving connections per room | JavaScript / V8 (baseline) | 672 (1.00×) | 672 (1.00×) | 96.44 (1.00×) | 225.09 (1.00×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + JS bindings | 685 (1.02×) | 685 (1.02×) | 86.13 (0.89×) | 160.99 (0.72×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + direct host calls | 638 (0.95×) | 638 (0.95×) | 137.90 (1.43×) | 263.13 (1.17×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / Wasmtime + direct host calls | 665 (0.99×) | 665 (0.99×) | 159.82 (1.66×) | 283.97 (1.26×) |
| 5 rooms, 8 receiving connections per room | Native Rust | 654 (0.97×) | 654 (0.97×) | 174.16 (1.81×) | 244.05 (1.08×) |


## Process startup

### Compile Wasm in each new process

Each runtime starts in 10 fresh processes from the same initial room data. The runner polls for a validated home page every 1 ms, then requests the first room's HTML page. Filesystem caches stay warm; compiled Wasm code is shared only within each process. The room response includes object activation, storage opening, and application initialization. Columns report medians independently, so the median parts need not add up to the median total. Memory is sampled after the first room response.

| Application runtime | Start → home page response (ms) | Home page → first room response (ms) | Start → first room response (ms); min–max across trials | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 254.33 (1.00×) | 29.94 (1.00×) | 282.89 (1.00×)<br>266.11–307.37 | 39.6 (1.00×) |
| Rust Wasm / V8 + JS bindings | 255.74 (1.01×) | 36.41 (1.22×) | 292.76 (1.03×)<br>282.57–309.82 | 41.4 (1.05×) |
| Rust Wasm / V8 + direct host calls | 228.76 (0.90×) | 24.05 (0.80×) | 254.23 (0.90×)<br>248.93–278.08 | 30.6 (0.77×) |
| Rust Wasm / Wasmtime + direct host calls | 297.17 (1.17×) | 22.20 (0.74×) | 318.56 (1.13×)<br>312.64–338.52 | 31.8 (0.80×) |
| Native Rust | 226.00 (0.89×) | 21.80 (0.73×) | 247.11 (0.87×)<br>242.42–272.68 | 24.7 (0.62×) |

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

Wasm module compilation/loading: **66.21 ms → 1.03 ms** when reusing compiled code. All 10 cached trials hit the cache. Load tests exclude startup and compilation. [Raw data](compile-cache/cold-cache-results.json).

## Correctness checks and recorded sources

The `results.json`, `cold-results.json`, and `validation.json` records contain **75 HTTP trials, 30 WebSocket trials, and 50 startup trials**, plus **3,580 dedicated full-response checks**. The HTTP load clients completed 3,753,723 requests, including 31,332 posts. The WebSocket sessions added 8,275 posts, 144,699 verified message deliveries, and 144,699 verified HTTP read confirmations. All trials passed response and ordering checks; all 105 post-trial audits passed across all 50 room databases. Source hashes agree across these three records, and captured files match their recorded SHA-256 hashes.

The `compile-cache/` comparison contains **60 additional startup trials**. Its referenced source files match their recorded SHA-256 hashes.

The load tests started at 2026-10-16T01:14:29Z and completed at 2026-10-16T01:34:01Z. The load-test JSON records the before/after thermal checks. The largest recorded load-client CPU usage was 67.0% of one core; clients ran in separate processes on the same machine.

An independent Python implementation produces expected response bytes from the initial data and confirmed posts. Dedicated validation and warmup compare complete bodies. The HTTP load client checks every read's full-body length and CRC32, verifies write acknowledgments, and requires unique write versions with no gaps. A read must include any write confirmed before that read was sent. After each load trial the runner checks every room's current page and audits every SQLite message, timestamp, user, unread count, room version, and `PRAGMA integrity_check`.

Dedicated checks cover invalid credentials, malformed login types and membership, cache invalidation, multiple users, 50 maximum-length messages, HTML expanding beyond 1 MiB, and 8 KiB messages whose JSON encoding exceeds 32 KiB. Restart validation deletes celld's entire local runtime directory and recovers all rooms from the object store, retaining the checked messages and unread changes.

The full data are [load tests and resource use](results.json), [startup](cold-results.json), and [correctness](validation.json). They contain SHA-256 hashes of the measured binaries, guest, application, adapters, clients, and relevant sources and lockfiles. The `source_snapshot` field identifies the captured source files. The `recorded_only` map identifies hashes whose corresponding files are not distributed with the data. Build, test, measurement, and report scripts live under `bench/`. Run phases and build commands are in the [reproduction guide](../../REPRODUCE.md).

## What this benchmark covers

The direct adapters implement this application's buffered HTTP, SQL, object routing, and WebSocket needs. They are not a general implementation of every Workers API. Native Rust is a trusted-code performance reference and does not provide an untrusted-code sandbox. The Wasm adapters enforce memory ranges and object storage scope, but these runs do not include production tenant CPU interruption or prove hostile-tenant isolation. This is a single-node, local persistence study; it does not measure a fleet, remote-bucket latency, network failure, or global placement.

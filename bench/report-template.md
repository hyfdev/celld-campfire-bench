# celld-campfire-bench: JavaScript, Rust Wasm, and native Rust

## The application and execution paths

The measured application is a Workers chat service with a stateless entry handler and one named Durable Object per room. It provides demo login, room membership, HTML and JSON history, posting, unread counts, page caching, and live WebSocket broadcasts. Each of 50 rooms starts with 1,000 messages. Reads authenticate a user, check membership, and select the latest 50 messages. Posting parses JSON and inserts a message; a SQLite trigger increments the room version counter and the other members' unread counts. Messages include Chinese, emoji, URLs, newlines, and HTML characters requiring escaping.

This is a small Campfire-style application implemented for this study. The native Rust reference is the same Rust business source compiled into celld. Celld itself is a native executable in every mode; only the Rust application's Wasm variants are compiled to Wasm. The runnable application and architecture diagram are in [the README](../../README.md).

All modes run in celld and share its request admission, named-object routing, room scheduling, SQLite cursors, WebSocket registry, and persistence system. Celld holds responses and broadcasts until storage confirms the writes they depend on. Persistence uses a local SQLite-backed object store on one node.

EXECUTION_TABLE

The **Wasmtime + direct host calls** mode handles requests and bindings in Rust and Wasm, with no JavaScript execution. The chat handlers compile to Wasm; SQLite runs outside Wasm. Requests, responses, and typed SQL rows use the `postcard` binary format. SQL transfer buffers are 512 KiB; Wasmtime limits linear memory to 128 MiB.

The **V8 + direct host calls** mode executes the same Wasm module. Its imports call Rust callbacks through V8's function/value interface, with no JS source execution.

JS_BRIDGE_DESCRIPTIONThese are complete application measurements. JS bindings use cursor wrappers and per-socket sends. Direct Rust bindings transfer typed SQL rows and provide a host broadcast operation. The results include request handling, binding APIs, data conversion, runtime bookkeeping, and generated machine code.

## Measurement method

MACHINE_SETUP The host/native release build uses optimization level 3, thin LTO, and 16 codegen units. The Wasm build uses optimization level 3, full LTO, panic abort, SIMD128, and bulk-memory instructions. Wasmtime uses Cranelift's speed optimization and compiles its module once per process. Native Rust uses the compiler's default host CPU target.

The request executor uses one Tokio thread. The Worker entry has one runtime instance, and each room runtime instance holds up to 32 Durable Objects. Storage, object ownership, and engine helper threads remain active, so the process can use more than one CPU core. Each object allows up to 256 requests in flight; the HTTP load tests use at most HTTP_IN_FLIGHT across the server. Every load trial starts a fresh process from the same initial room data.

HTTP tests use HTTP_CONCURRENCIES simultaneous clients for TARGET_SECONDS seconds, then wait for outstanding responses. Each client waits for its complete response before sending another request. ROUND_SETUP Warmup checks 2,000 reads across every room, four users, and both formats. Posting and WebSocket cases also warm 1,000 writes, 20 per room. The HTML/JSON tests labeled without response caching generate each response from the database; database and operating-system caches remain active. Cached reads still authenticate, check membership, and read the room version: three application SQL calls; a miss adds the message query. The cache limit is 1 MiB per room and all writes invalidate its version.

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

CPU is user plus system time of the server process from `proc_pid_rusage`, with Mach ticks converted using the recorded timebase of MACH_TIMEBASE. CPU time/request or CPU time/message divides that time by the successful request or new-message count. CPU percentage uses one full core as 100%; helper threads can make it exceed 100%. Faster implementations can have a larger total CPU percentage because they finish more work. CPU time/request or CPU time/message shows the CPU used for the same unit of work. Load-client CPU is recorded separately.

Memory is the whole server process, including engine state, application caches, SQLite, and HTTP/WebSocket buffers. The primary memory number is the maximum sampled physical footprint, sampled every 100 ms and at both endpoints. RSS is also recorded. Load-test tables report the median of PRESSURE_ROUNDS trial maxima, in MiB (1,048,576 bytes). Read-only trials retain identical data; write trials add different numbers of rows at different achieved throughputs.

HTTP response time runs from sending a request to receiving its complete body, including time spent waiting behind other requests. p99 means 99% of measured requests finished within that time. The results show capacity with the stated number of clients; those clients wait for responses, so they reduce their sending rate when the server slows down. Tables report medians of trial values, with minimum–maximum throughput ranges.

## Results

RESULTS_TABLES

## Process startup

### Compile Wasm in each new process

Each runtime starts in STARTUP_ROUNDS fresh processes from the same initial room data. The runner polls for a validated home page every 1 ms, then requests the first room's HTML page. Filesystem caches stay warm; compiled Wasm code is shared only within each process. The room response includes object activation, storage opening, and application initialization. Columns report medians independently, so the median parts need not add up to the median total. Memory is sampled after the first room response.

STARTUP_TABLE

COMPILE_CACHE_RESULTS

## Correctness checks and recorded sources

CORRECTNESS_SUMMARY

An independent Python implementation produces expected response bytes from the initial data and confirmed posts. Dedicated validation and warmup compare complete bodies. The HTTP load client checks every read's full-body length and CRC32, verifies write acknowledgments, and requires unique write versions with no gaps. A read must include any write confirmed before that read was sent. After each load trial the runner checks every room's current page and audits every SQLite message, timestamp, user, unread count, room version, and `PRAGMA integrity_check`.

Dedicated checks cover invalid credentials, malformed login types and membership, cache invalidation, multiple users, 50 maximum-length messages, HTML expanding beyond 1 MiB, and 8 KiB messages whose JSON encoding exceeds 32 KiB. Restart validation deletes celld's entire local runtime directory and recovers all rooms from the object store, retaining the checked messages and unread changes.

The full data are [load tests and resource use](results.json), [startup](cold-results.json), and [correctness](validation.json). They contain SHA-256 hashes of the measured binaries, guest, application, adapters, clients, and relevant sources and lockfiles. The `source_snapshot` field identifies the captured source files. The `recorded_only` map identifies hashes whose corresponding files are not distributed with the data. Build, test, measurement, and report scripts live under `bench/`. Run phases and build commands are in the [reproduction guide](../../REPRODUCE.md).

## What this benchmark covers

The direct adapters implement this application's buffered HTTP, SQL, object routing, and WebSocket needs. They are not a general implementation of every Workers API. Native Rust is a trusted-code performance reference and does not provide an untrusted-code sandbox. The Wasm adapters enforce memory ranges and object storage scope, but these runs do not include production tenant CPU interruption or prove hostile-tenant isolation. This is a single-node, local persistence study; it does not measure a fleet, remote-bucket latency, network failure, or global placement.

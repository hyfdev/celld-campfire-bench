# celld-campfire-bench: JavaScript, Rust Wasm, and native Rust

RESULTS_SUMMARY

## The application and execution paths

The measured application is a Workers chat service with a stateless entry handler and one named Durable Object per room. It provides demo login, room membership, HTML and JSON history, posting, unread counts, page caching, and live WebSocket broadcasts. Each of 50 rooms starts with 1,000 messages. Reads authenticate a user, check membership, and select the latest 50 messages. Posting parses JSON and inserts a message; a SQLite trigger increments the room version and the other members' unread counts. Messages include Chinese, emoji, URLs, newlines, and HTML characters requiring escaping.

This is a small Campfire-style application implemented for this study. The native Rust reference is the same Rust business source compiled into celld. Celld itself is a native executable in every mode; only the Rust application's Wasm variants are compiled to Wasm. The runnable application, architecture diagram, and build commands are in [the README](../../README.md).

All four celld variants run in the actual celld daemon and share its stateless admission, named-object routing, ownership, activation, cell scheduling, SQLite cursors, write/observed positions, WebSocket registry, LTX log persistence, and output gates. They use the same SQL, schema, data, and cache policy. Output gates hold a response or broadcast until celld has confirmed the writes it depends on. For this single-node test, the persistence target is celld's SQLite-backed local object store.

| Label | Application execution | Binding interface |
| --- | --- | --- |
| celld JS | JavaScript in V8 152.1.0 | Existing Worker and Durable Object APIs |
| celld Wasm | Rust Wasm in Wasmtime 49.0.0 | Typed Wasm imports calling Rust directly |
| celld native | The same Rust business source compiled natively | Direct Rust trait calls |
| celld V8 Wasm | The same Wasm module in V8 152.1.0 | Native Rust callbacks through V8's function/value API |

The **celld Wasm** request path executes no JavaScript and uses no JS function/value ABI. Rust receives HTTP or WebSocket input, calls the Wasm entry or room handler, and services its SQL and socket imports in Rust. It uses neither workers-rs nor wasm-bindgen. Request/response records and SQL rows use postcard; byte payloads use bulk binary copies. SQL transfer buffers are 512 KiB and Wasm linear memory is limited to 128 MiB.

The **celld V8 Wasm** path executes no JS source or application shim, but it retains V8's native `Function` callback and value-conversion interface. Its results compare a second implementation of Wasm hosting. They do not isolate compiler quality from the host-call interface.

The comparison measures complete application execution paths, including their binding adapters. JavaScript uses cursor wrappers and per-socket sends; the Rust adapters transfer typed SQL rows and expose a host broadcast operation. The application does its authentication, permission checks, parsing, rendering, and cache management in its selected language. Differences include runtime/API bookkeeping and data conversion as well as generated machine code. This is not an isolated measurement of the cost of one JS bridge.

## Measurement

MACHINE_SETUP The host/native release build uses optimization level 3, thin LTO, and 16 codegen units. The Wasm build uses optimization level 3, full LTO, panic abort, SIMD128, and bulk-memory instructions. Wasmtime uses Cranelift's speed optimization and compiles its module once per process. Native Rust uses the compiler's default host CPU target.

Celld uses one Tokio executor thread, one stateless slot, and its existing 32-cells-per-slot placement policy. Its ownership actor, blocking I/O, and engine helper threads remain active. This is not a process restricted to one CPU core. The per-cell in-flight limit is 256, above the test's maximum HTTP_IN_FLIGHT HTTP requests in flight. Every timed trial starts a fresh process and restores the same seeded persistent snapshot.

Each HTTP case runs at HTTP_CONCURRENCIES simultaneous closed-loop clients for TARGET_SECONDS seconds, followed by draining its outstanding requests. ROUND_SETUP Warmup checks 2,000 reads across every room, four users, and both formats. Posting and WebSocket cases also warm 1,000 writes, 20 per room. No page-cache warmup is applied to the uncached cases. Cached reads still authenticate, check membership, and read the room version: three application SQL calls; a miss adds the message query. The cache limit is 1 MiB per room and all writes invalidate its version.

| Case | Work |
| --- | --- |
| Uncached HTML | Select and render the latest 50 messages for every request |
| Uncached JSON | Select and serialize the latest 50 messages for every request |
| Cached HTML | Authenticate and check the version, then return cached page bytes |
| Post | Authenticate, parse JSON, insert, update version/unread counts, wait for durability |
| Mixed | Cached reads, 80% HTML / 20% JSON, plus one writer targeting 100 posts/s |

The mixed writer skips missed timer ticks and waits for each acknowledgment. Its actual rate and the resulting cache-hit ratio are reported. Mixed tests therefore do not impose an identical completed read/write ratio on every engine. The fixed read-only cases are the cleaner comparison of equal work per request.

WebSocket tests use one producer per active room: either one room with 32 listeners, or five rooms with eight listeners each. Producers also receive their own broadcasts, for 33 or 45 total sockets. Producers use user 1 and listeners use user 2; the tests stress connections rather than distinct user accounts. Each participant marks the room read when joining and sends an HTTP read acknowledgment after every message, as the delivered browser UI does. A read acknowledgment authenticates, checks membership, clears that user's unread count, increments the room version, and waits for durability. Joining and its initial acknowledgments finish before timing.

Each producer waits for its broadcast and all participants' completed read acknowledgments before posting again. This bounds the session round to at most 33 read-acknowledgment requests per room; the browser UI can overlap acknowledgments across consecutive messages. The test reproduces each message's server work, while using a bounded closed-loop producer. Listeners verify the message text, IDs, timestamp, and room-version step. All read-acknowledgment versions must form the exact expected unique sequence. The sender's broadcast p99 and the complete-round p99 are both reported. CPU per message includes broadcasts and HTTP read acknowledgments. Listeners have a final 200 ms drain before delivery counts are checked.

CPU is user plus system time of the server process from `proc_pid_rusage`, with Mach ticks converted using the recorded timebase of MACH_TIMEBASE. CPU per operation divides that time by the successful request or message count. CPU percentage uses one full core as 100%; helper threads can make it exceed 100%. Faster implementations can have a larger total CPU percentage because they finish more work. Compare CPU time per operation to assess equal-work efficiency. Load-client CPU is recorded separately.

Memory is the whole server process, including engine state, application caches, SQLite, and HTTP/WebSocket buffers. The primary memory number is the maximum sampled physical footprint, sampled every 100 ms and at both endpoints. RSS is also recorded. Pressure tables report the median of PRESSURE_ROUNDS trial maxima, in MiB (1,048,576 bytes). Read-only trials retain identical data; write trials add different numbers of rows at different achieved throughputs.

HTTP latency covers dispatch through receipt of the complete response, including waiting behind other requests. These are closed-loop capacity measurements at the stated concurrency, not latency guarantees under a fixed offered request rate. Tables report medians of trial throughputs, CPU values, memory maxima, and p99 values. Throughput ranges show the minimum and maximum of those trials.

## Results

RESULTS_TABLES

## Startup

Each engine is tested STARTUP_ROUNDS times with a fresh service process and the seeded persistent snapshot. The runner polls for a validated root response at 1 ms intervals, then requests and validates the first room's HTML page. Filesystem caches stay warm. The Wasmtime module cache lasts only inside each process; there is no serialized/AOT cache between trials. The first-room column includes object activation, storage opening, application initialization, and the HTTP response after the root is ready. The total column measures complete process launch through that room response, rather than an isolated function cold start. Values are medians of STARTUP_ROUNDS trials; time columns are aggregated independently. The memory reading is taken after that first page, rather than the maximum of a timed pressure run.

STARTUP_TABLE

## Correctness and provenance

CORRECTNESS_SUMMARY

The independent Python fixture produces expected response bytes from the seed and acknowledged posts. Dedicated validation and warmup compare complete bodies. The HTTP load client checks every read's full-body length and CRC32, verifies write acknowledgments, and requires unique, contiguous write versions. Reads must not predate writes acknowledged before their dispatch. After each trial the runner checks every room's current page and audits every SQLite message, timestamp, user, unread count, room version, and `PRAGMA integrity_check`.

Dedicated checks cover invalid credentials, malformed login types and membership, cache invalidation, multiple users, 50 maximum-length messages, HTML expanding beyond 1 MiB, and 8 KiB messages whose JSON encoding exceeds 32 KiB. Restart validation deletes celld's entire local runtime directory and recovers all rooms from the object store, retaining the checked messages and unread changes.

The full data are [pressure and resources](results.json), [startup](cold-results.json), and [correctness](validation.json). They contain SHA-256 hashes of the measured binaries, guest, application, adapters, clients, and relevant sources and lockfiles. The `source_snapshot` field identifies the captured source files. The `recorded_only` map identifies binary and original-harness hashes whose bytes are not distributed with the data. The portable harness lives under `bench/`. Run phases and build commands are in [the README](../../README.md).

## Practical limits

The direct adapters implement this application's buffered HTTP, SQL, object routing, and WebSocket needs. They are not a general implementation of every Workers API. Native Rust is a trusted-code performance reference and does not provide an untrusted-code sandbox. The Wasm adapters enforce memory ranges and object storage scope, but these runs do not include production tenant CPU interruption or prove hostile-tenant isolation. This is a single-node, local persistence study; it does not measure a fleet, remote-bucket latency, network failure, or global placement.

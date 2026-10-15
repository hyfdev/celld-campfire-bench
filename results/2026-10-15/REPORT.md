# celld-campfire-bench: JavaScript, Rust Wasm, and native Rust

At 32 concurrent uncached HTML reads, Rust Wasm serves **8,066 requests/s versus JavaScript's 2,573 (3.14×)**. Server CPU time per request is **159.00 µs versus 439.80 µs**; whole-process physical memory is **51.0 MiB versus 113.7 MiB**. Native Rust serves 10,771 requests/s; Wasm reaches 75% of that throughput.

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

Tests run on Apple M4 Pro with 14 logical CPUs, 48 GiB RAM, and `macOS-26.5.2-arm64-arm-64bit-Mach-O`. The celld base is commit `90b43017241f81189453d326d05948f388b34652`. The compiler is `rustc 1.96.0 (ac68faa20 2026-05-25)`. The host/native release build uses optimization level 3, thin LTO, and 16 codegen units. The Wasm build uses optimization level 3, full LTO, panic abort, SIMD128, and bulk-memory instructions. Wasmtime uses Cranelift's speed optimization and compiles its module once per process. Native Rust uses the compiler's default host CPU target.

Celld uses one Tokio executor thread, one stateless slot, and its existing 32-cells-per-slot placement policy. Its ownership actor, blocking I/O, and engine helper threads remain active. This is not a process restricted to one CPU core. The per-cell in-flight limit is 256, above the test's maximum 129 HTTP requests in flight. Every timed trial starts a fresh process and restores the same seeded persistent snapshot.

Each HTTP case runs at 1, 32, 128 simultaneous closed-loop clients for 5 seconds, followed by draining its outstanding requests. Tests use 3 rounds. The four engines run sequentially; their order rotates between rounds. Warmup checks 2,000 reads across every room, four users, and both formats. Posting and WebSocket cases also warm 1,000 writes, 20 per room. No page-cache warmup is applied to the uncached cases. Cached reads still authenticate, check membership, and read the room version: three application SQL calls; a miss adds the message query. The cache limit is 1 MiB per room and all writes invalidate its version.

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

CPU is user plus system time of the server process from `proc_pid_rusage`, with Mach ticks converted using the recorded timebase of 125/3. CPU per operation divides that time by the successful request or message count. CPU percentage uses one full core as 100%; helper threads can make it exceed 100%. Faster implementations can have a larger total CPU percentage because they finish more work. Compare CPU time per operation to assess equal-work efficiency. Load-client CPU is recorded separately.

Memory is the whole server process, including engine state, application caches, SQLite, and HTTP/WebSocket buffers. The primary memory number is the maximum sampled physical footprint, sampled every 100 ms and at both endpoints. RSS is also recorded. Pressure tables report the median of 3 trial maxima, in MiB (1,048,576 bytes). Read-only trials retain identical data; write trials add different numbers of rows at different achieved throughputs.

HTTP latency covers dispatch through receipt of the complete response, including waiting behind other requests. These are closed-loop capacity measurements at the stated concurrency, not latency guarantees under a fixed offered request rate. Tables report medians of trial throughputs, CPU values, memory maxima, and p99 values. Throughput ranges show the minimum and maximum of those trials.

## Results

### A comparable read: 32 concurrent clients, latest 50 messages as HTML

Lower CPU time, memory, and latency are better; higher requests/s is better. The memory column covers the entire server process. Each value is a median across the recorded trials.

| Execution | Requests/s | Server CPU µs/request | Physical memory MiB | p99 response ms |
| --- | ---: | ---: | ---: | ---: |
| celld JS | 2,573 | 439.80 | 113.7 | 18.94 |
| celld Wasm | 8,066 | 159.00 | 51.0 | 5.71 |
| celld native | 10,771 | 124.81 | 46.2 | 4.45 |

At the same concurrency, Wasm has 3.42× JavaScript's uncached JSON throughput and 5.11× its cached HTML throughput. The additional V8 Wasm measurements appear in the complete tables below.

### Uncached HTML reads

| Clients | Execution | Requests/s (trial range) | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 2,125 (2,124–2,156) | 435.93 | 92.6 | 110.1 | 0.80 |
| 1 | celld Wasm | 5,029 (4,924–5,408) | 170.52 | 87.4 | 46.6 | 0.44 |
| 1 | celld native | 6,558 (6,172–6,648) | 132.36 | 86.8 | 44.8 | 0.29 |
| 1 | celld V8 Wasm | 5,281 (4,784–5,300) | 168.20 | 88.8 | 59.8 | 0.35 |
| 32 | celld JS | 2,573 (2,485–2,639) | 439.80 | 113.1 | 113.7 | 18.94 |
| 32 | celld Wasm | 8,066 (7,916–8,250) | 159.00 | 128.2 | 51.0 | 5.71 |
| 32 | celld native | 10,771 (10,390–11,002) | 124.81 | 134.4 | 46.2 | 4.45 |
| 32 | celld V8 Wasm | 7,696 (7,666–7,905) | 163.80 | 126.1 | 65.2 | 6.46 |
| 128 | celld JS | 2,586 (2,453–2,604) | 461.24 | 119.4 | 122.5 | 61.36 |
| 128 | celld Wasm | 8,125 (7,950–8,223) | 157.16 | 127.9 | 57.8 | 22.62 |
| 128 | celld native | 11,110 (10,652–11,187) | 121.17 | 134.6 | 56.9 | 16.16 |
| 128 | celld V8 Wasm | 7,812 (7,489–7,830) | 163.37 | 126.9 | 71.3 | 23.65 |

### Uncached JSON reads

| Clients | Execution | Requests/s (trial range) | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 2,436 (2,435–2,492) | 374.25 | 91.2 | 116.3 | 0.71 |
| 1 | celld Wasm | 6,410 (6,282–6,512) | 135.98 | 86.8 | 49.4 | 0.31 |
| 1 | celld native | 7,648 (7,643–7,693) | 111.60 | 85.3 | 45.4 | 0.26 |
| 1 | celld V8 Wasm | 6,656 (6,559–6,729) | 129.61 | 86.3 | 63.6 | 0.30 |
| 32 | celld JS | 3,215 (3,169–3,216) | 359.27 | 114.8 | 119.3 | 14.08 |
| 32 | celld Wasm | 10,988 (10,957–11,017) | 121.36 | 133.4 | 50.8 | 4.39 |
| 32 | celld native | 14,258 (14,204–14,535) | 100.85 | 143.8 | 52.1 | 3.45 |
| 32 | celld V8 Wasm | 11,320 (10,701–11,501) | 119.80 | 135.2 | 69.9 | 4.23 |
| 128 | celld JS | 3,014 (2,801–3,091) | 402.00 | 121.1 | 129.3 | 50.66 |
| 128 | celld Wasm | 10,785 (10,721–10,927) | 124.53 | 134.5 | 54.8 | 16.73 |
| 128 | celld native | 14,244 (14,180–14,290) | 100.89 | 143.7 | 57.3 | 12.08 |
| 128 | celld V8 Wasm | 11,131 (10,699–11,344) | 123.09 | 137.0 | 74.3 | 16.20 |

### Cached HTML reads

| Clients | Execution | Requests/s (trial range) | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 3,258 (3,241–3,386) | 265.08 | 87.4 | 123.1 | 0.56 |
| 1 | celld Wasm | 9,993 (9,235–10,065) | 80.44 | 80.2 | 59.8 | 0.21 |
| 1 | celld native | 10,500 (10,170–10,554) | 76.53 | 80.2 | 54.8 | 0.21 |
| 1 | celld V8 Wasm | 9,729 (9,200–9,869) | 82.48 | 80.2 | 70.7 | 0.23 |
| 32 | celld JS | 5,051 (5,001–5,097) | 244.14 | 123.3 | 133.5 | 8.70 |
| 32 | celld Wasm | 25,808 (25,351–26,193) | 69.42 | 178.7 | 66.8 | 1.95 |
| 32 | celld native | 29,312 (28,852–29,797) | 62.25 | 182.6 | 61.6 | 1.73 |
| 32 | celld V8 Wasm | 25,174 (24,860–25,583) | 70.53 | 177.6 | 78.2 | 2.02 |
| 128 | celld JS | 4,730 (4,725–4,732) | 284.24 | 134.3 | 141.8 | 34.02 |
| 128 | celld Wasm | 25,104 (24,632–25,408) | 70.82 | 177.8 | 73.7 | 6.92 |
| 128 | celld native | 28,434 (27,217–28,516) | 63.70 | 181.1 | 69.9 | 6.33 |
| 128 | celld V8 Wasm | 23,757 (23,162–24,559) | 74.04 | 175.9 | 84.5 | 7.52 |

### Durable HTTP posts

Every request posts a message and waits for the runtime's durability gate.

| Clients | Execution | Requests/s (trial range) | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 130 (124–134) | 2738.79 | 35.5 | 108.1 | 14.43 |
| 1 | celld Wasm | 130 (121–136) | 2675.50 | 34.6 | 60.8 | 16.31 |
| 1 | celld native | 128 (124–133) | 2743.23 | 34.8 | 53.8 | 15.08 |
| 1 | celld V8 Wasm | 129 (104–133) | 2713.12 | 34.9 | 72.9 | 16.40 |
| 32 | celld JS | 356 (286–374) | 2228.57 | 79.4 | 112.7 | 698.54 |
| 32 | celld Wasm | 377 (313–408) | 1800.73 | 72.8 | 64.6 | 813.39 |
| 32 | celld native | 368 (360–382) | 1833.87 | 67.0 | 58.5 | 725.24 |
| 32 | celld V8 Wasm | 337 (327–378) | 1790.71 | 67.2 | 77.5 | 719.84 |
| 128 | celld JS | 735 (699–816) | 1550.50 | 113.9 | 134.2 | 940.05 |
| 128 | celld Wasm | 730 (726–818) | 1445.53 | 107.0 | 75.8 | 882.34 |
| 128 | celld native | 710 (689–752) | 1511.46 | 106.9 | 78.1 | 1094.58 |
| 128 | celld V8 Wasm | 799 (748–867) | 1461.41 | 116.8 | 95.0 | 871.80 |

### Cached reads with a concurrent writer

The request rate includes reads and posts. CPU/op uses that combined count. The separate writer and cache-hit table follows.

| Clients | Execution | Requests/s (trial range) | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 2,719 (2,706–2,767) | 367.36 | 99.4 | 142.2 | 6.89 |
| 1 | celld Wasm | 6,746 (6,641–8,131) | 109.64 | 78.7 | 77.4 | 5.40 |
| 1 | celld native | 7,419 (6,111–8,976) | 103.31 | 76.6 | 64.3 | 5.55 |
| 1 | celld V8 Wasm | 6,953 (5,638–7,629) | 106.60 | 74.1 | 90.8 | 5.62 |
| 32 | celld JS | 4,679 (4,397–4,702) | 287.06 | 134.3 | 147.7 | 14.80 |
| 32 | celld Wasm | 23,483 (21,854–23,637) | 79.84 | 186.5 | 87.1 | 7.23 |
| 32 | celld native | 26,907 (25,487–27,187) | 72.02 | 193.8 | 79.5 | 6.78 |
| 32 | celld V8 Wasm | 22,787 (21,230–23,533) | 81.69 | 186.6 | 101.5 | 7.21 |
| 128 | celld JS | 4,437 (4,331–4,585) | 313.52 | 138.0 | 159.2 | 37.80 |
| 128 | celld Wasm | 25,049 (24,198–25,054) | 73.63 | 184.3 | 98.5 | 9.47 |
| 128 | celld native | 28,806 (28,443–28,808) | 65.81 | 189.6 | 88.0 | 8.60 |
| 128 | celld V8 Wasm | 24,458 (23,428–24,585) | 74.85 | 183.1 | 111.3 | 9.99 |

| Read clients | Execution | Reads/s | Posts/s | Cache hits % |
| ---: | --- | ---: | ---: | ---: |
| 1 | celld JS | 2,619 | 99.9 | 92.4 |
| 1 | celld Wasm | 6,646 | 99.9 | 97.0 |
| 1 | celld native | 7,320 | 99.9 | 97.3 |
| 1 | celld V8 Wasm | 6,853 | 99.9 | 97.1 |
| 32 | celld JS | 4,610 | 68.5 | 94.1 |
| 32 | celld Wasm | 23,384 | 99.7 | 98.3 |
| 32 | celld native | 26,807 | 99.9 | 98.5 |
| 32 | celld V8 Wasm | 22,687 | 99.9 | 98.2 |
| 128 | celld JS | 4,409 | 27.4 | 97.6 |
| 128 | celld Wasm | 24,985 | 63.5 | 99.0 |
| 128 | celld native | 28,733 | 73.3 | 99.0 |
| 128 | celld V8 Wasm | 24,396 | 62.1 | 99.0 |

### Live WebSocket messages

Each message is persisted and broadcast, then every participant sends an HTTP read acknowledgment. A producer waits for all these requests before its next message. Frame deliveries include the sender. CPU/message includes fanout and the read acknowledgments. The two p99 columns separate sender broadcast acknowledgment from the complete round.

| Rooms × listeners | Execution | Messages/s (trial range) | Frame deliveries/s | HTTP read acks/s | CPU µs/message | CPU % | Physical MiB | Sender p99 ms | Complete round p99 ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1x32 | celld JS | 40 (39–40) | 1,320 | 1,320 | 45446.79 | 180.5 | 123.3 | 15.05 | 36.75 |
| 1x32 | celld Wasm | 42 (42–43) | 1,397 | 1,397 | 42457.56 | 179.7 | 67.7 | 14.50 | 32.69 |
| 1x32 | celld native | 42 (40–42) | 1,375 | 1,375 | 40792.51 | 171.6 | 64.3 | 15.45 | 33.45 |
| 1x32 | celld V8 Wasm | 41 (40–43) | 1,355 | 1,355 | 42306.30 | 174.9 | 79.7 | 14.47 | 34.63 |
| 5x8 | celld JS | 76 (76–77) | 688 | 688 | 14687.94 | 112.1 | 116.9 | 105.39 | 184.29 |
| 5x8 | celld Wasm | 75 (73–76) | 671 | 671 | 12890.32 | 95.5 | 51.8 | 153.94 | 221.24 |
| 5x8 | celld native | 76 (75–79) | 686 | 686 | 12629.47 | 98.1 | 58.8 | 127.00 | 253.71 |
| 5x8 | celld V8 Wasm | 77 (75–79) | 691 | 691 | 12685.22 | 97.4 | 78.3 | 119.56 | 232.30 |

## Startup

Each engine is tested 10 times with a fresh service process and the seeded persistent snapshot. The runner polls for a validated root response at 1 ms intervals, then requests and validates the first room's HTML page. Filesystem caches stay warm. The Wasmtime module cache lasts only inside each process; there is no serialized/AOT cache between trials. The first-room column includes object activation, storage opening, application initialization, and the HTTP response after the root is ready. The total column measures complete process launch through that room response, rather than an isolated function cold start. Values are medians of 10 trials; time columns are aggregated independently. The memory reading is taken after that first page, rather than the maximum of a timed pressure run.

| Execution | Process → root ms | First room after root ms | Process → first page ms (range) | Physical MiB after first page |
| --- | ---: | ---: | ---: | ---: |
| celld JS | 257.66 | 30.74 | 288.80 (275.67–301.24) | 39.2 |
| celld Wasm | 308.38 | 23.36 | 331.33 (327.76–345.39) | 30.7 |
| celld native | 237.37 | 23.80 | 261.19 (252.39–273.42) | 24.6 |
| celld V8 Wasm | 237.18 | 25.62 | 262.38 (259.88–273.62) | 30.8 |

## Correctness and provenance

The reported measurements contain **180 HTTP trials, 24 WebSocket trials, and 40 startup trials**, plus **2,852 dedicated full-response checks**. The HTTP pressure clients completed 8,515,105 operations, including 90,791 posts. The WebSocket sessions added 7,077 posts, 123,141 verified frame deliveries, and 123,141 verified HTTP read acknowledgments. All trials passed response and ordering checks; all 204 post-trial audits passed across all 50 room databases. Source hashes agree across validation, pressure, and startup. Captured source files match their recorded SHA-256 hashes; `recorded_only` lists the executable and original-harness digests retained as provenance.

The pressure run started at 2026-10-15T05:01:30Z and completed at 2026-10-15T05:41:42Z. The pressure JSON records the before/after thermal checks. The largest recorded load-client CPU usage was 68.3% of one core; clients ran in separate processes on the same machine.

The independent Python fixture produces expected response bytes from the seed and acknowledged posts. Dedicated validation and warmup compare complete bodies. The HTTP load client checks every read's full-body length and CRC32, verifies write acknowledgments, and requires unique, contiguous write versions. Reads must not predate writes acknowledged before their dispatch. After each trial the runner checks every room's current page and audits every SQLite message, timestamp, user, unread count, room version, and `PRAGMA integrity_check`.

Dedicated checks cover invalid credentials, malformed login types and membership, cache invalidation, multiple users, 50 maximum-length messages, HTML expanding beyond 1 MiB, and 8 KiB messages whose JSON encoding exceeds 32 KiB. Restart validation deletes celld's entire local runtime directory and recovers all rooms from the object store, retaining the checked messages and unread changes.

The full data are [pressure and resources](results.json), [startup](cold-results.json), and [correctness](validation.json). They contain SHA-256 hashes of the measured binaries, guest, application, adapters, clients, and relevant sources and lockfiles. The `source_snapshot` field identifies the captured source files. The `recorded_only` map identifies binary and original-harness hashes whose bytes are not distributed with the data. The portable harness lives under `bench/`. Run phases and build commands are in [the README](../../README.md).

## Practical limits

The direct adapters implement this application's buffered HTTP, SQL, object routing, and WebSocket needs. They are not a general implementation of every Workers API. Native Rust is a trusted-code performance reference and does not provide an untrusted-code sandbox. The Wasm adapters enforce memory ranges and object storage scope, but these runs do not include production tenant CPU interruption or prove hostile-tenant isolation. This is a single-node, local persistence study; it does not measure a fleet, remote-bucket latency, network failure, or global placement.

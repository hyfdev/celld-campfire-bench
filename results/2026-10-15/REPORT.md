# celld-campfire-bench: JavaScript, Rust Wasm, and native Rust

With in-memory page caching disabled and 32 concurrent clients, Rust Wasm serves **8,066 requests/s versus JavaScript's 2,573 (3.14×)**. Server CPU time per request is **159.00 µs versus 439.80 µs (0.36×)**; whole-process physical memory is **51.0 MiB versus 113.7 MiB (0.45×)**. Native Rust serves 10,771 requests/s (4.19× JavaScript's throughput).

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

Each HTTP case runs at 1, 32, 128 simultaneous closed-loop clients for 5 seconds, followed by draining its outstanding requests. Each client waits for its complete response before sending the next request; 32 clients allow up to 32 requests in flight. Tests use 3 rounds. The four engines run sequentially; their order rotates between rounds. Warmup checks 2,000 reads across every room, four users, and both formats. Posting and WebSocket cases also warm 1,000 writes, 20 per room. The uncached cases bypass the application's generated-response cache; database and operating-system caches remain active. Cached reads still authenticate, check membership, and read the room version: three application SQL calls; a miss adds the message query. The cache limit is 1 MiB per room and all writes invalidate its version.

| Case | Work |
| --- | --- |
| Server-rendered HTML (without in-memory page caching) | Select and render the latest 50 messages for every request |
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

Each measurement cell shows **absolute value (ratio to JS)** for the same workload and client count, with JS at **1.00×**. A memory ratio of **0.45×** means 45% of JS's memory. Ratios use the unrounded medians; trial ranges remain absolute. Higher throughput is better; lower CPU time, memory, latency, and startup time are better. CPU % ratios describe total CPU use at each engine's achieved throughput.

### Server-rendered HTML (without in-memory page caching): 32 concurrent clients

Each request selects and renders the latest 50 messages. The memory column covers the entire server process. Each value is a median across the recorded trials.

| Execution | Requests/s | Server CPU µs/request | Physical memory MiB | p99 response ms |
| --- | ---: | ---: | ---: | ---: |
| celld JS | 2,573 (1.00×) | 439.80 (1.00×) | 113.7 (1.00×) | 18.94 (1.00×) |
| celld Wasm | 8,066 (3.14×) | 159.00 (0.36×) | 51.0 (0.45×) | 5.71 (0.30×) |
| celld native | 10,771 (4.19×) | 124.81 (0.28×) | 46.2 (0.41×) | 4.45 (0.23×) |

At the same concurrency, Wasm has 3.42× JavaScript's uncached JSON throughput and 5.11× its cached HTML throughput. The additional V8 Wasm measurements appear in the complete tables below.

### Server-rendered HTML (without in-memory page caching)

| Clients | Execution | Requests/s; trial range | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 2,125 (1.00×)<br>2,124–2,156 | 435.93 (1.00×) | 92.6 (1.00×) | 110.1 (1.00×) | 0.80 (1.00×) |
| 1 | celld Wasm | 5,029 (2.37×)<br>4,924–5,408 | 170.52 (0.39×) | 87.4 (0.94×) | 46.6 (0.42×) | 0.44 (0.56×) |
| 1 | celld native | 6,558 (3.09×)<br>6,172–6,648 | 132.36 (0.30×) | 86.8 (0.94×) | 44.8 (0.41×) | 0.29 (0.36×) |
| 1 | celld V8 Wasm | 5,281 (2.49×)<br>4,784–5,300 | 168.20 (0.39×) | 88.8 (0.96×) | 59.8 (0.54×) | 0.35 (0.44×) |
| 32 | celld JS | 2,573 (1.00×)<br>2,485–2,639 | 439.80 (1.00×) | 113.1 (1.00×) | 113.7 (1.00×) | 18.94 (1.00×) |
| 32 | celld Wasm | 8,066 (3.14×)<br>7,916–8,250 | 159.00 (0.36×) | 128.2 (1.13×) | 51.0 (0.45×) | 5.71 (0.30×) |
| 32 | celld native | 10,771 (4.19×)<br>10,390–11,002 | 124.81 (0.28×) | 134.4 (1.19×) | 46.2 (0.41×) | 4.45 (0.23×) |
| 32 | celld V8 Wasm | 7,696 (2.99×)<br>7,666–7,905 | 163.80 (0.37×) | 126.1 (1.11×) | 65.2 (0.57×) | 6.46 (0.34×) |
| 128 | celld JS | 2,586 (1.00×)<br>2,453–2,604 | 461.24 (1.00×) | 119.4 (1.00×) | 122.5 (1.00×) | 61.36 (1.00×) |
| 128 | celld Wasm | 8,125 (3.14×)<br>7,950–8,223 | 157.16 (0.34×) | 127.9 (1.07×) | 57.8 (0.47×) | 22.62 (0.37×) |
| 128 | celld native | 11,110 (4.30×)<br>10,652–11,187 | 121.17 (0.26×) | 134.6 (1.13×) | 56.9 (0.46×) | 16.16 (0.26×) |
| 128 | celld V8 Wasm | 7,812 (3.02×)<br>7,489–7,830 | 163.37 (0.35×) | 126.9 (1.06×) | 71.3 (0.58×) | 23.65 (0.39×) |

### Uncached JSON reads

| Clients | Execution | Requests/s; trial range | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 2,436 (1.00×)<br>2,435–2,492 | 374.25 (1.00×) | 91.2 (1.00×) | 116.3 (1.00×) | 0.71 (1.00×) |
| 1 | celld Wasm | 6,410 (2.63×)<br>6,282–6,512 | 135.98 (0.36×) | 86.8 (0.95×) | 49.4 (0.42×) | 0.31 (0.44×) |
| 1 | celld native | 7,648 (3.14×)<br>7,643–7,693 | 111.60 (0.30×) | 85.3 (0.94×) | 45.4 (0.39×) | 0.26 (0.37×) |
| 1 | celld V8 Wasm | 6,656 (2.73×)<br>6,559–6,729 | 129.61 (0.35×) | 86.3 (0.95×) | 63.6 (0.55×) | 0.30 (0.43×) |
| 32 | celld JS | 3,215 (1.00×)<br>3,169–3,216 | 359.27 (1.00×) | 114.8 (1.00×) | 119.3 (1.00×) | 14.08 (1.00×) |
| 32 | celld Wasm | 10,988 (3.42×)<br>10,957–11,017 | 121.36 (0.34×) | 133.4 (1.16×) | 50.8 (0.43×) | 4.39 (0.31×) |
| 32 | celld native | 14,258 (4.44×)<br>14,204–14,535 | 100.85 (0.28×) | 143.8 (1.25×) | 52.1 (0.44×) | 3.45 (0.24×) |
| 32 | celld V8 Wasm | 11,320 (3.52×)<br>10,701–11,501 | 119.80 (0.33×) | 135.2 (1.18×) | 69.9 (0.59×) | 4.23 (0.30×) |
| 128 | celld JS | 3,014 (1.00×)<br>2,801–3,091 | 402.00 (1.00×) | 121.1 (1.00×) | 129.3 (1.00×) | 50.66 (1.00×) |
| 128 | celld Wasm | 10,785 (3.58×)<br>10,721–10,927 | 124.53 (0.31×) | 134.5 (1.11×) | 54.8 (0.42×) | 16.73 (0.33×) |
| 128 | celld native | 14,244 (4.73×)<br>14,180–14,290 | 100.89 (0.25×) | 143.7 (1.19×) | 57.3 (0.44×) | 12.08 (0.24×) |
| 128 | celld V8 Wasm | 11,131 (3.69×)<br>10,699–11,344 | 123.09 (0.31×) | 137.0 (1.13×) | 74.3 (0.57×) | 16.20 (0.32×) |

### Cached HTML reads

| Clients | Execution | Requests/s; trial range | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 3,258 (1.00×)<br>3,241–3,386 | 265.08 (1.00×) | 87.4 (1.00×) | 123.1 (1.00×) | 0.56 (1.00×) |
| 1 | celld Wasm | 9,993 (3.07×)<br>9,235–10,065 | 80.44 (0.30×) | 80.2 (0.92×) | 59.8 (0.49×) | 0.21 (0.38×) |
| 1 | celld native | 10,500 (3.22×)<br>10,170–10,554 | 76.53 (0.29×) | 80.2 (0.92×) | 54.8 (0.44×) | 0.21 (0.36×) |
| 1 | celld V8 Wasm | 9,729 (2.99×)<br>9,200–9,869 | 82.48 (0.31×) | 80.2 (0.92×) | 70.7 (0.57×) | 0.23 (0.40×) |
| 32 | celld JS | 5,051 (1.00×)<br>5,001–5,097 | 244.14 (1.00×) | 123.3 (1.00×) | 133.5 (1.00×) | 8.70 (1.00×) |
| 32 | celld Wasm | 25,808 (5.11×)<br>25,351–26,193 | 69.42 (0.28×) | 178.7 (1.45×) | 66.8 (0.50×) | 1.95 (0.22×) |
| 32 | celld native | 29,312 (5.80×)<br>28,852–29,797 | 62.25 (0.25×) | 182.6 (1.48×) | 61.6 (0.46×) | 1.73 (0.20×) |
| 32 | celld V8 Wasm | 25,174 (4.98×)<br>24,860–25,583 | 70.53 (0.29×) | 177.6 (1.44×) | 78.2 (0.59×) | 2.02 (0.23×) |
| 128 | celld JS | 4,730 (1.00×)<br>4,725–4,732 | 284.24 (1.00×) | 134.3 (1.00×) | 141.8 (1.00×) | 34.02 (1.00×) |
| 128 | celld Wasm | 25,104 (5.31×)<br>24,632–25,408 | 70.82 (0.25×) | 177.8 (1.32×) | 73.7 (0.52×) | 6.92 (0.20×) |
| 128 | celld native | 28,434 (6.01×)<br>27,217–28,516 | 63.70 (0.22×) | 181.1 (1.35×) | 69.9 (0.49×) | 6.33 (0.19×) |
| 128 | celld V8 Wasm | 23,757 (5.02×)<br>23,162–24,559 | 74.04 (0.26×) | 175.9 (1.31×) | 84.5 (0.60×) | 7.52 (0.22×) |

### Durable HTTP posts

Every request posts a message and waits for the runtime's durability gate.

| Clients | Execution | Requests/s; trial range | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 130 (1.00×)<br>124–134 | 2738.79 (1.00×) | 35.5 (1.00×) | 108.1 (1.00×) | 14.43 (1.00×) |
| 1 | celld Wasm | 130 (1.00×)<br>121–136 | 2675.50 (0.98×) | 34.6 (0.97×) | 60.8 (0.56×) | 16.31 (1.13×) |
| 1 | celld native | 128 (0.98×)<br>124–133 | 2743.23 (1.00×) | 34.8 (0.98×) | 53.8 (0.50×) | 15.08 (1.04×) |
| 1 | celld V8 Wasm | 129 (0.99×)<br>104–133 | 2713.12 (0.99×) | 34.9 (0.98×) | 72.9 (0.67×) | 16.40 (1.14×) |
| 32 | celld JS | 356 (1.00×)<br>286–374 | 2228.57 (1.00×) | 79.4 (1.00×) | 112.7 (1.00×) | 698.54 (1.00×) |
| 32 | celld Wasm | 377 (1.06×)<br>313–408 | 1800.73 (0.81×) | 72.8 (0.92×) | 64.6 (0.57×) | 813.39 (1.16×) |
| 32 | celld native | 368 (1.03×)<br>360–382 | 1833.87 (0.82×) | 67.0 (0.84×) | 58.5 (0.52×) | 725.24 (1.04×) |
| 32 | celld V8 Wasm | 337 (0.95×)<br>327–378 | 1790.71 (0.80×) | 67.2 (0.85×) | 77.5 (0.69×) | 719.84 (1.03×) |
| 128 | celld JS | 735 (1.00×)<br>699–816 | 1550.50 (1.00×) | 113.9 (1.00×) | 134.2 (1.00×) | 940.05 (1.00×) |
| 128 | celld Wasm | 730 (0.99×)<br>726–818 | 1445.53 (0.93×) | 107.0 (0.94×) | 75.8 (0.56×) | 882.34 (0.94×) |
| 128 | celld native | 710 (0.97×)<br>689–752 | 1511.46 (0.97×) | 106.9 (0.94×) | 78.1 (0.58×) | 1094.58 (1.16×) |
| 128 | celld V8 Wasm | 799 (1.09×)<br>748–867 | 1461.41 (0.94×) | 116.8 (1.02×) | 95.0 (0.71×) | 871.80 (0.93×) |

### Cached reads with a concurrent writer

The request rate includes reads and posts. CPU/op uses that combined count. The separate writer and cache-hit table follows.

| Clients | Execution | Requests/s; trial range | CPU µs/op | CPU % | Physical MiB | p99 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | celld JS | 2,719 (1.00×)<br>2,706–2,767 | 367.36 (1.00×) | 99.4 (1.00×) | 142.2 (1.00×) | 6.89 (1.00×) |
| 1 | celld Wasm | 6,746 (2.48×)<br>6,641–8,131 | 109.64 (0.30×) | 78.7 (0.79×) | 77.4 (0.54×) | 5.40 (0.78×) |
| 1 | celld native | 7,419 (2.73×)<br>6,111–8,976 | 103.31 (0.28×) | 76.6 (0.77×) | 64.3 (0.45×) | 5.55 (0.80×) |
| 1 | celld V8 Wasm | 6,953 (2.56×)<br>5,638–7,629 | 106.60 (0.29×) | 74.1 (0.75×) | 90.8 (0.64×) | 5.62 (0.82×) |
| 32 | celld JS | 4,679 (1.00×)<br>4,397–4,702 | 287.06 (1.00×) | 134.3 (1.00×) | 147.7 (1.00×) | 14.80 (1.00×) |
| 32 | celld Wasm | 23,483 (5.02×)<br>21,854–23,637 | 79.84 (0.28×) | 186.5 (1.39×) | 87.1 (0.59×) | 7.23 (0.49×) |
| 32 | celld native | 26,907 (5.75×)<br>25,487–27,187 | 72.02 (0.25×) | 193.8 (1.44×) | 79.5 (0.54×) | 6.78 (0.46×) |
| 32 | celld V8 Wasm | 22,787 (4.87×)<br>21,230–23,533 | 81.69 (0.28×) | 186.6 (1.39×) | 101.5 (0.69×) | 7.21 (0.49×) |
| 128 | celld JS | 4,437 (1.00×)<br>4,331–4,585 | 313.52 (1.00×) | 138.0 (1.00×) | 159.2 (1.00×) | 37.80 (1.00×) |
| 128 | celld Wasm | 25,049 (5.65×)<br>24,198–25,054 | 73.63 (0.23×) | 184.3 (1.34×) | 98.5 (0.62×) | 9.47 (0.25×) |
| 128 | celld native | 28,806 (6.49×)<br>28,443–28,808 | 65.81 (0.21×) | 189.6 (1.37×) | 88.0 (0.55×) | 8.60 (0.23×) |
| 128 | celld V8 Wasm | 24,458 (5.51×)<br>23,428–24,585 | 74.85 (0.24×) | 183.1 (1.33×) | 111.3 (0.70×) | 9.99 (0.26×) |

| Read clients | Execution | Reads/s | Posts/s | Cache hits % |
| ---: | --- | ---: | ---: | ---: |
| 1 | celld JS | 2,619 (1.00×) | 99.9 (1.00×) | 92.4 (1.00×) |
| 1 | celld Wasm | 6,646 (2.54×) | 99.9 (1.00×) | 97.0 (1.05×) |
| 1 | celld native | 7,320 (2.80×) | 99.9 (1.00×) | 97.3 (1.05×) |
| 1 | celld V8 Wasm | 6,853 (2.62×) | 99.9 (1.00×) | 97.1 (1.05×) |
| 32 | celld JS | 4,610 (1.00×) | 68.5 (1.00×) | 94.1 (1.00×) |
| 32 | celld Wasm | 23,384 (5.07×) | 99.7 (1.46×) | 98.3 (1.05×) |
| 32 | celld native | 26,807 (5.82×) | 99.9 (1.46×) | 98.5 (1.05×) |
| 32 | celld V8 Wasm | 22,687 (4.92×) | 99.9 (1.46×) | 98.2 (1.04×) |
| 128 | celld JS | 4,409 (1.00×) | 27.4 (1.00×) | 97.6 (1.00×) |
| 128 | celld Wasm | 24,985 (5.67×) | 63.5 (2.32×) | 99.0 (1.01×) |
| 128 | celld native | 28,733 (6.52×) | 73.3 (2.67×) | 99.0 (1.01×) |
| 128 | celld V8 Wasm | 24,396 (5.53×) | 62.1 (2.26×) | 99.0 (1.01×) |

### Live WebSocket messages

Each message is persisted and broadcast, then every participant sends an HTTP read acknowledgment. A producer waits for all these requests before its next message. Frame deliveries include the sender. CPU/message includes fanout and the read acknowledgments. The two p99 columns separate sender broadcast acknowledgment from the complete round.

| Rooms × listeners | Execution | Messages/s; trial range | Frame deliveries/s | HTTP read acks/s | CPU µs/message | CPU % | Physical MiB | Sender p99 ms | Complete round p99 ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1x32 | celld JS | 40 (1.00×)<br>39–40 | 1,320 (1.00×) | 1,320 (1.00×) | 45446.79 (1.00×) | 180.5 (1.00×) | 123.3 (1.00×) | 15.05 (1.00×) | 36.75 (1.00×) |
| 1x32 | celld Wasm | 42 (1.06×)<br>42–43 | 1,397 (1.06×) | 1,397 (1.06×) | 42457.56 (0.93×) | 179.7 (1.00×) | 67.7 (0.55×) | 14.50 (0.96×) | 32.69 (0.89×) |
| 1x32 | celld native | 42 (1.04×)<br>40–42 | 1,375 (1.04×) | 1,375 (1.04×) | 40792.51 (0.90×) | 171.6 (0.95×) | 64.3 (0.52×) | 15.45 (1.03×) | 33.45 (0.91×) |
| 1x32 | celld V8 Wasm | 41 (1.03×)<br>40–43 | 1,355 (1.03×) | 1,355 (1.03×) | 42306.30 (0.93×) | 174.9 (0.97×) | 79.7 (0.65×) | 14.47 (0.96×) | 34.63 (0.94×) |
| 5x8 | celld JS | 76 (1.00×)<br>76–77 | 688 (1.00×) | 688 (1.00×) | 14687.94 (1.00×) | 112.1 (1.00×) | 116.9 (1.00×) | 105.39 (1.00×) | 184.29 (1.00×) |
| 5x8 | celld Wasm | 75 (0.98×)<br>73–76 | 671 (0.98×) | 671 (0.98×) | 12890.32 (0.88×) | 95.5 (0.85×) | 51.8 (0.44×) | 153.94 (1.46×) | 221.24 (1.20×) |
| 5x8 | celld native | 76 (1.00×)<br>75–79 | 686 (1.00×) | 686 (1.00×) | 12629.47 (0.86×) | 98.1 (0.87×) | 58.8 (0.50×) | 127.00 (1.21×) | 253.71 (1.38×) |
| 5x8 | celld V8 Wasm | 77 (1.00×)<br>75–79 | 691 (1.00×) | 691 (1.00×) | 12685.22 (0.86×) | 97.4 (0.87×) | 78.3 (0.67×) | 119.56 (1.13×) | 232.30 (1.26×) |

## Startup

Each engine is tested 10 times with a fresh service process and the seeded persistent snapshot. The runner polls for a validated root response at 1 ms intervals, then requests and validates the first room's HTML page. Filesystem caches stay warm. The Wasmtime module cache lasts only inside each process; there is no serialized/AOT cache between trials. The first-room column includes object activation, storage opening, application initialization, and the HTTP response after the root is ready. The total column measures complete process launch through that room response, rather than an isolated function cold start. Values are medians of 10 trials; time columns are aggregated independently. The memory reading is taken after that first page, rather than the maximum of a timed pressure run.

| Execution | Process → root ms | First room after root ms | Process → first page ms; trial range | Physical MiB after first page |
| --- | ---: | ---: | ---: | ---: |
| celld JS | 257.66 (1.00×) | 30.74 (1.00×) | 288.80 (1.00×)<br>275.67–301.24 | 39.2 (1.00×) |
| celld Wasm | 308.38 (1.20×) | 23.36 (0.76×) | 331.33 (1.15×)<br>327.76–345.39 | 30.7 (0.78×) |
| celld native | 237.37 (0.92×) | 23.80 (0.77×) | 261.19 (0.90×)<br>252.39–273.42 | 24.6 (0.63×) |
| celld V8 Wasm | 237.18 (0.92×) | 25.62 (0.83×) | 262.38 (0.91×)<br>259.88–273.62 | 30.8 (0.79×) |

## Correctness and provenance

The reported measurements contain **180 HTTP trials, 24 WebSocket trials, and 40 startup trials**, plus **2,852 dedicated full-response checks**. The HTTP pressure clients completed 8,515,105 operations, including 90,791 posts. The WebSocket sessions added 7,077 posts, 123,141 verified frame deliveries, and 123,141 verified HTTP read acknowledgments. All trials passed response and ordering checks; all 204 post-trial audits passed across all 50 room databases. Source hashes agree across validation, pressure, and startup. Captured source files match their recorded SHA-256 hashes; `recorded_only` lists the executable and original-harness digests retained as provenance.

The pressure run started at 2026-10-15T05:01:30Z and completed at 2026-10-15T05:41:42Z. The pressure JSON records the before/after thermal checks. The largest recorded load-client CPU usage was 68.3% of one core; clients ran in separate processes on the same machine.

The independent Python fixture produces expected response bytes from the seed and acknowledged posts. Dedicated validation and warmup compare complete bodies. The HTTP load client checks every read's full-body length and CRC32, verifies write acknowledgments, and requires unique, contiguous write versions. Reads must not predate writes acknowledged before their dispatch. After each trial the runner checks every room's current page and audits every SQLite message, timestamp, user, unread count, room version, and `PRAGMA integrity_check`.

Dedicated checks cover invalid credentials, malformed login types and membership, cache invalidation, multiple users, 50 maximum-length messages, HTML expanding beyond 1 MiB, and 8 KiB messages whose JSON encoding exceeds 32 KiB. Restart validation deletes celld's entire local runtime directory and recovers all rooms from the object store, retaining the checked messages and unread changes.

The full data are [pressure and resources](results.json), [startup](cold-results.json), and [correctness](validation.json). They contain SHA-256 hashes of the measured binaries, guest, application, adapters, clients, and relevant sources and lockfiles. The `source_snapshot` field identifies the captured source files. The `recorded_only` map identifies binary and original-harness hashes whose bytes are not distributed with the data. The portable harness lives under `bench/`. Run phases and build commands are in [the README](../../README.md).

## Practical limits

The direct adapters implement this application's buffered HTTP, SQL, object routing, and WebSocket needs. They are not a general implementation of every Workers API. Native Rust is a trusted-code performance reference and does not provide an untrusted-code sandbox. The Wasm adapters enforce memory ranges and object storage scope, but these runs do not include production tenant CPU interruption or prove hostile-tenant isolation. This is a single-node, local persistence study; it does not measure a fleet, remote-bucket latency, network failure, or global placement.

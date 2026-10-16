# celld-campfire-bench: JavaScript, Rust Wasm, and native Rust

## The application and execution paths

The measured application is a Workers chat service with a stateless entry handler and one named Durable Object per room. It provides demo login, room membership, HTML and JSON history, posting, unread counts, page caching, and live WebSocket broadcasts. Each of 50 rooms starts with 1,000 messages. Reads authenticate a user, check membership, and select the latest 50 messages. Posting parses JSON and inserts a message; a SQLite trigger increments the room version counter and the other members' unread counts. Messages include Chinese, emoji, URLs, newlines, and HTML characters requiring escaping.

This is a small Campfire-style application implemented for this study. The native Rust reference is the same Rust business source compiled into celld. Celld itself is a native executable in every mode; only the Rust application's Wasm variants are compiled to Wasm. The runnable application and architecture diagram are in [the README](../../README.md).

All modes run in celld and share its request admission, named-object routing, room scheduling, SQLite cursors, WebSocket registry, and persistence system. Celld holds responses and broadcasts until storage confirms the writes they depend on. Persistence uses a local SQLite-backed object store on one node.

| Application runtime | Application code | SQL and WebSocket access |
| --- | --- | --- |
| JavaScript / V8 (baseline) | JavaScript in V8 (`v8` crate 152.1.0) | JS Worker and Durable Object APIs |
| Rust Wasm / V8 + direct host calls | Rust Wasm in V8 (`v8` crate 152.1.0) | Rust callbacks through V8; no JS source execution |
| Rust Wasm / Wasmtime + direct host calls | Rust Wasm in Wasmtime 49.0.0 | Wasm imports calling Rust directly |
| Native Rust | The same Rust source compiled natively | Direct Rust calls |

The **Wasmtime + direct host calls** mode handles requests and bindings in Rust and Wasm, with no JavaScript execution. The chat handlers compile to Wasm; SQLite runs outside Wasm. Requests, responses, and typed SQL rows use the `postcard` binary format. SQL transfer buffers are 512 KiB; Wasmtime limits linear memory to 128 MiB.

The **V8 + direct host calls** mode executes the same Wasm module. Its imports call Rust callbacks through V8's function/value interface, with no JS source execution.

These are complete application measurements. JS bindings use cursor wrappers and per-socket sends. Direct Rust bindings transfer typed SQL rows and provide a host broadcast operation. The results include request handling, binding APIs, data conversion, runtime bookkeeping, and generated machine code.

## Measurement method

Tests run on Apple M4 Pro with 14 logical CPUs, 48 GiB RAM, and macOS 26.5.2 arm64. The celld base is commit `90b43017241f81189453d326d05948f388b34652`. The compiler is `rustc 1.96.0 (ac68faa20 2026-05-25)`. The host/native release build uses optimization level 3, thin LTO, and 16 codegen units. The Wasm build uses optimization level 3, full LTO, panic abort, SIMD128, and bulk-memory instructions. Wasmtime uses Cranelift's speed optimization and compiles its module once per process. Native Rust uses the compiler's default host CPU target.

The request executor uses one Tokio thread. The Worker entry has one runtime instance, and each room runtime instance holds up to 32 Durable Objects. Storage, object ownership, and engine helper threads remain active, so the process can use more than one CPU core. Each object allows up to 256 requests in flight; the HTTP load tests use at most 129 across the server. Every load trial starts a fresh process from the same initial room data.

HTTP tests use 1, 32, 128 simultaneous clients for 5 seconds, then wait for outstanding responses. Each client waits for its complete response before sending another request. Tests use 3 rounds. The 4 runtimes run one at a time; their order rotates between rounds. Warmup checks 2,000 reads across every room, four users, and both formats. Posting and WebSocket cases also warm 1,000 writes, 20 per room. The HTML/JSON tests labeled without response caching generate each response from the database; database and operating-system caches remain active. Cached reads still authenticate, check membership, and read the room version: three application SQL calls; a miss adds the message query. The cache limit is 1 MiB per room and all writes invalidate its version.

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
| 1 | JavaScript / V8 (baseline) | 2,125 (1.00×)<br>2,124–2,156 | 435.93 (1.00×) | 92.6 (1.00×) | 110.1 (1.00×) | 0.80 (1.00×) |
| 1 | Rust Wasm / V8 + direct host calls | 5,281 (2.49×)<br>4,784–5,300 | 168.20 (0.39×) | 88.8 (0.96×) | 59.8 (0.54×) | 0.35 (0.44×) |
| 1 | Rust Wasm / Wasmtime + direct host calls | 5,029 (2.37×)<br>4,924–5,408 | 170.52 (0.39×) | 87.4 (0.94×) | 46.6 (0.42×) | 0.44 (0.56×) |
| 1 | Native Rust | 6,558 (3.09×)<br>6,172–6,648 | 132.36 (0.30×) | 86.8 (0.94×) | 44.8 (0.41×) | 0.29 (0.36×) |
| 32 | JavaScript / V8 (baseline) | 2,573 (1.00×)<br>2,485–2,639 | 439.80 (1.00×) | 113.1 (1.00×) | 113.7 (1.00×) | 18.94 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 7,696 (2.99×)<br>7,666–7,905 | 163.80 (0.37×) | 126.1 (1.11×) | 65.2 (0.57×) | 6.46 (0.34×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 8,066 (3.14×)<br>7,916–8,250 | 159.00 (0.36×) | 128.2 (1.13×) | 51.0 (0.45×) | 5.71 (0.30×) |
| 32 | Native Rust | 10,771 (4.19×)<br>10,390–11,002 | 124.81 (0.28×) | 134.4 (1.19×) | 46.2 (0.41×) | 4.45 (0.23×) |
| 128 | JavaScript / V8 (baseline) | 2,586 (1.00×)<br>2,453–2,604 | 461.24 (1.00×) | 119.4 (1.00×) | 122.5 (1.00×) | 61.36 (1.00×) |
| 128 | Rust Wasm / V8 + direct host calls | 7,812 (3.02×)<br>7,489–7,830 | 163.37 (0.35×) | 126.9 (1.06×) | 71.3 (0.58×) | 23.65 (0.39×) |
| 128 | Rust Wasm / Wasmtime + direct host calls | 8,125 (3.14×)<br>7,950–8,223 | 157.16 (0.34×) | 127.9 (1.07×) | 57.8 (0.47×) | 22.62 (0.37×) |
| 128 | Native Rust | 11,110 (4.30×)<br>10,652–11,187 | 121.17 (0.26×) | 134.6 (1.13×) | 56.9 (0.46×) | 16.16 (0.26×) |

### Read messages as JSON (no response cache)

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | JavaScript / V8 (baseline) | 2,436 (1.00×)<br>2,435–2,492 | 374.25 (1.00×) | 91.2 (1.00×) | 116.3 (1.00×) | 0.71 (1.00×) |
| 1 | Rust Wasm / V8 + direct host calls | 6,656 (2.73×)<br>6,559–6,729 | 129.61 (0.35×) | 86.3 (0.95×) | 63.6 (0.55×) | 0.30 (0.43×) |
| 1 | Rust Wasm / Wasmtime + direct host calls | 6,410 (2.63×)<br>6,282–6,512 | 135.98 (0.36×) | 86.8 (0.95×) | 49.4 (0.42×) | 0.31 (0.44×) |
| 1 | Native Rust | 7,648 (3.14×)<br>7,643–7,693 | 111.60 (0.30×) | 85.3 (0.94×) | 45.4 (0.39×) | 0.26 (0.37×) |
| 32 | JavaScript / V8 (baseline) | 3,215 (1.00×)<br>3,169–3,216 | 359.27 (1.00×) | 114.8 (1.00×) | 119.3 (1.00×) | 14.08 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 11,320 (3.52×)<br>10,701–11,501 | 119.80 (0.33×) | 135.2 (1.18×) | 69.9 (0.59×) | 4.23 (0.30×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 10,988 (3.42×)<br>10,957–11,017 | 121.36 (0.34×) | 133.4 (1.16×) | 50.8 (0.43×) | 4.39 (0.31×) |
| 32 | Native Rust | 14,258 (4.44×)<br>14,204–14,535 | 100.85 (0.28×) | 143.8 (1.25×) | 52.1 (0.44×) | 3.45 (0.24×) |
| 128 | JavaScript / V8 (baseline) | 3,014 (1.00×)<br>2,801–3,091 | 402.00 (1.00×) | 121.1 (1.00×) | 129.3 (1.00×) | 50.66 (1.00×) |
| 128 | Rust Wasm / V8 + direct host calls | 11,131 (3.69×)<br>10,699–11,344 | 123.09 (0.31×) | 137.0 (1.13×) | 74.3 (0.57×) | 16.20 (0.32×) |
| 128 | Rust Wasm / Wasmtime + direct host calls | 10,785 (3.58×)<br>10,721–10,927 | 124.53 (0.31×) | 134.5 (1.11×) | 54.8 (0.42×) | 16.73 (0.33×) |
| 128 | Native Rust | 14,244 (4.73×)<br>14,180–14,290 | 100.89 (0.25×) | 143.7 (1.19×) | 57.3 (0.44×) | 12.08 (0.24×) |

### Server-rendered HTML (with in-memory page caching)

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | JavaScript / V8 (baseline) | 3,258 (1.00×)<br>3,241–3,386 | 265.08 (1.00×) | 87.4 (1.00×) | 123.1 (1.00×) | 0.56 (1.00×) |
| 1 | Rust Wasm / V8 + direct host calls | 9,729 (2.99×)<br>9,200–9,869 | 82.48 (0.31×) | 80.2 (0.92×) | 70.7 (0.57×) | 0.23 (0.40×) |
| 1 | Rust Wasm / Wasmtime + direct host calls | 9,993 (3.07×)<br>9,235–10,065 | 80.44 (0.30×) | 80.2 (0.92×) | 59.8 (0.49×) | 0.21 (0.38×) |
| 1 | Native Rust | 10,500 (3.22×)<br>10,170–10,554 | 76.53 (0.29×) | 80.2 (0.92×) | 54.8 (0.44×) | 0.21 (0.36×) |
| 32 | JavaScript / V8 (baseline) | 5,051 (1.00×)<br>5,001–5,097 | 244.14 (1.00×) | 123.3 (1.00×) | 133.5 (1.00×) | 8.70 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 25,174 (4.98×)<br>24,860–25,583 | 70.53 (0.29×) | 177.6 (1.44×) | 78.2 (0.59×) | 2.02 (0.23×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 25,808 (5.11×)<br>25,351–26,193 | 69.42 (0.28×) | 178.7 (1.45×) | 66.8 (0.50×) | 1.95 (0.22×) |
| 32 | Native Rust | 29,312 (5.80×)<br>28,852–29,797 | 62.25 (0.25×) | 182.6 (1.48×) | 61.6 (0.46×) | 1.73 (0.20×) |
| 128 | JavaScript / V8 (baseline) | 4,730 (1.00×)<br>4,725–4,732 | 284.24 (1.00×) | 134.3 (1.00×) | 141.8 (1.00×) | 34.02 (1.00×) |
| 128 | Rust Wasm / V8 + direct host calls | 23,757 (5.02×)<br>23,162–24,559 | 74.04 (0.26×) | 175.9 (1.31×) | 84.5 (0.60×) | 7.52 (0.22×) |
| 128 | Rust Wasm / Wasmtime + direct host calls | 25,104 (5.31×)<br>24,632–25,408 | 70.82 (0.25×) | 177.8 (1.32×) | 73.7 (0.52×) | 6.92 (0.20×) |
| 128 | Native Rust | 28,434 (6.01×)<br>27,217–28,516 | 63.70 (0.22×) | 181.1 (1.35×) | 69.9 (0.49×) | 6.33 (0.19×) |

### Post a message; reply after saving it

| Simultaneous clients | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | JavaScript / V8 (baseline) | 130 (1.00×)<br>124–134 | 2738.79 (1.00×) | 35.5 (1.00×) | 108.1 (1.00×) | 14.43 (1.00×) |
| 1 | Rust Wasm / V8 + direct host calls | 129 (0.99×)<br>104–133 | 2713.12 (0.99×) | 34.9 (0.98×) | 72.9 (0.67×) | 16.40 (1.14×) |
| 1 | Rust Wasm / Wasmtime + direct host calls | 130 (1.00×)<br>121–136 | 2675.50 (0.98×) | 34.6 (0.97×) | 60.8 (0.56×) | 16.31 (1.13×) |
| 1 | Native Rust | 128 (0.98×)<br>124–133 | 2743.23 (1.00×) | 34.8 (0.98×) | 53.8 (0.50×) | 15.08 (1.04×) |
| 32 | JavaScript / V8 (baseline) | 356 (1.00×)<br>286–374 | 2228.57 (1.00×) | 79.4 (1.00×) | 112.7 (1.00×) | 698.54 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 337 (0.95×)<br>327–378 | 1790.71 (0.80×) | 67.2 (0.85×) | 77.5 (0.69×) | 719.84 (1.03×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 377 (1.06×)<br>313–408 | 1800.73 (0.81×) | 72.8 (0.92×) | 64.6 (0.57×) | 813.39 (1.16×) |
| 32 | Native Rust | 368 (1.03×)<br>360–382 | 1833.87 (0.82×) | 67.0 (0.84×) | 58.5 (0.52×) | 725.24 (1.04×) |
| 128 | JavaScript / V8 (baseline) | 735 (1.00×)<br>699–816 | 1550.50 (1.00×) | 113.9 (1.00×) | 134.2 (1.00×) | 940.05 (1.00×) |
| 128 | Rust Wasm / V8 + direct host calls | 799 (1.09×)<br>748–867 | 1461.41 (0.94×) | 116.8 (1.02×) | 95.0 (0.71×) | 871.80 (0.93×) |
| 128 | Rust Wasm / Wasmtime + direct host calls | 730 (0.99×)<br>726–818 | 1445.53 (0.93×) | 107.0 (0.94×) | 75.8 (0.56×) | 882.34 (0.94×) |
| 128 | Native Rust | 710 (0.97×)<br>689–752 | 1511.46 (0.97×) | 106.9 (0.94×) | 78.1 (0.58×) | 1094.58 (1.16×) |

### Read message history while posting new messages

One additional client attempts 100 posts/s. Requests/s and CPU time/request include both reads and posts.

| Reading clients (+1 posting client) | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | JavaScript / V8 (baseline) | 2,719 (1.00×)<br>2,706–2,767 | 367.36 (1.00×) | 99.4 (1.00×) | 142.2 (1.00×) | 6.89 (1.00×) |
| 1 | Rust Wasm / V8 + direct host calls | 6,953 (2.56×)<br>5,638–7,629 | 106.60 (0.29×) | 74.1 (0.75×) | 90.8 (0.64×) | 5.62 (0.82×) |
| 1 | Rust Wasm / Wasmtime + direct host calls | 6,746 (2.48×)<br>6,641–8,131 | 109.64 (0.30×) | 78.7 (0.79×) | 77.4 (0.54×) | 5.40 (0.78×) |
| 1 | Native Rust | 7,419 (2.73×)<br>6,111–8,976 | 103.31 (0.28×) | 76.6 (0.77×) | 64.3 (0.45×) | 5.55 (0.80×) |
| 32 | JavaScript / V8 (baseline) | 4,679 (1.00×)<br>4,397–4,702 | 287.06 (1.00×) | 134.3 (1.00×) | 147.7 (1.00×) | 14.80 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 22,787 (4.87×)<br>21,230–23,533 | 81.69 (0.28×) | 186.6 (1.39×) | 101.5 (0.69×) | 7.21 (0.49×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 23,483 (5.02×)<br>21,854–23,637 | 79.84 (0.28×) | 186.5 (1.39×) | 87.1 (0.59×) | 7.23 (0.49×) |
| 32 | Native Rust | 26,907 (5.75×)<br>25,487–27,187 | 72.02 (0.25×) | 193.8 (1.44×) | 79.5 (0.54×) | 6.78 (0.46×) |
| 128 | JavaScript / V8 (baseline) | 4,437 (1.00×)<br>4,331–4,585 | 313.52 (1.00×) | 138.0 (1.00×) | 159.2 (1.00×) | 37.80 (1.00×) |
| 128 | Rust Wasm / V8 + direct host calls | 24,458 (5.51×)<br>23,428–24,585 | 74.85 (0.24×) | 183.1 (1.33×) | 111.3 (0.70×) | 9.99 (0.26×) |
| 128 | Rust Wasm / Wasmtime + direct host calls | 25,049 (5.65×)<br>24,198–25,054 | 73.63 (0.23×) | 184.3 (1.34×) | 98.5 (0.62×) | 9.47 (0.25×) |
| 128 | Native Rust | 28,806 (6.49×)<br>28,443–28,808 | 65.81 (0.21×) | 189.6 (1.37×) | 88.0 (0.55×) | 8.60 (0.23×) |

| Reading clients (+1 posting client) | Application runtime | Reads/s | Posts/s | Reads served from response cache (%) |
| ---: | --- | ---: | ---: | ---: |
| 1 | JavaScript / V8 (baseline) | 2,619 (1.00×) | 99.9 (1.00×) | 92.4 (1.00×) |
| 1 | Rust Wasm / V8 + direct host calls | 6,853 (2.62×) | 99.9 (1.00×) | 97.1 (1.05×) |
| 1 | Rust Wasm / Wasmtime + direct host calls | 6,646 (2.54×) | 99.9 (1.00×) | 97.0 (1.05×) |
| 1 | Native Rust | 7,320 (2.80×) | 99.9 (1.00×) | 97.3 (1.05×) |
| 32 | JavaScript / V8 (baseline) | 4,610 (1.00×) | 68.5 (1.00×) | 94.1 (1.00×) |
| 32 | Rust Wasm / V8 + direct host calls | 22,687 (4.92×) | 99.9 (1.46×) | 98.2 (1.04×) |
| 32 | Rust Wasm / Wasmtime + direct host calls | 23,384 (5.07×) | 99.7 (1.46×) | 98.3 (1.05×) |
| 32 | Native Rust | 26,807 (5.82×) | 99.9 (1.46×) | 98.5 (1.05×) |
| 128 | JavaScript / V8 (baseline) | 4,409 (1.00×) | 27.4 (1.00×) | 97.6 (1.00×) |
| 128 | Rust Wasm / V8 + direct host calls | 24,396 (5.53×) | 62.1 (2.26×) | 99.0 (1.01×) |
| 128 | Rust Wasm / Wasmtime + direct host calls | 24,985 (5.67×) | 63.5 (2.32×) | 99.0 (1.01×) |
| 128 | Native Rust | 28,733 (6.52×) | 73.3 (2.67×) | 99.0 (1.01×) |

### WebSocket chat throughput and server resources

| Rooms and receiving connections | Application runtime | Total messages/s; min–max across trials | CPU time/message (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 room, 32 receiving connections | JavaScript / V8 (baseline) | 40 (1.00×)<br>39–40 | 45446.79 (1.00×) | 180.5 (1.00×) | 123.3 (1.00×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + direct host calls | 41 (1.03×)<br>40–43 | 42306.30 (0.93×) | 174.9 (0.97×) | 79.7 (0.65×) |
| 1 room, 32 receiving connections | Rust Wasm / Wasmtime + direct host calls | 42 (1.06×)<br>42–43 | 42457.56 (0.93×) | 179.7 (1.00×) | 67.7 (0.55×) |
| 1 room, 32 receiving connections | Native Rust | 42 (1.04×)<br>40–42 | 40792.51 (0.90×) | 171.6 (0.95×) | 64.3 (0.52×) |
| 5 rooms, 8 receiving connections per room | JavaScript / V8 (baseline) | 76 (1.00×)<br>76–77 | 14687.94 (1.00×) | 112.1 (1.00×) | 116.9 (1.00×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + direct host calls | 77 (1.00×)<br>75–79 | 12685.22 (0.86×) | 97.4 (0.87×) | 78.3 (0.67×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / Wasmtime + direct host calls | 75 (0.98×)<br>73–76 | 12890.32 (0.88×) | 95.5 (0.85×) | 51.8 (0.44×) |
| 5 rooms, 8 receiving connections per room | Native Rust | 76 (1.00×)<br>75–79 | 12629.47 (0.86×) | 98.1 (0.87×) | 58.8 (0.50×) |

### WebSocket deliveries and read confirmations

| Rooms and receiving connections | Application runtime | Message deliveries/s | HTTP read confirmations/s | Send → sender receives message: p99 (ms) | Send → all read confirmations: p99 (ms) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 room, 32 receiving connections | JavaScript / V8 (baseline) | 1,320 (1.00×) | 1,320 (1.00×) | 15.05 (1.00×) | 36.75 (1.00×) |
| 1 room, 32 receiving connections | Rust Wasm / V8 + direct host calls | 1,355 (1.03×) | 1,355 (1.03×) | 14.47 (0.96×) | 34.63 (0.94×) |
| 1 room, 32 receiving connections | Rust Wasm / Wasmtime + direct host calls | 1,397 (1.06×) | 1,397 (1.06×) | 14.50 (0.96×) | 32.69 (0.89×) |
| 1 room, 32 receiving connections | Native Rust | 1,375 (1.04×) | 1,375 (1.04×) | 15.45 (1.03×) | 33.45 (0.91×) |
| 5 rooms, 8 receiving connections per room | JavaScript / V8 (baseline) | 688 (1.00×) | 688 (1.00×) | 105.39 (1.00×) | 184.29 (1.00×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / V8 + direct host calls | 691 (1.00×) | 691 (1.00×) | 119.56 (1.13×) | 232.30 (1.26×) |
| 5 rooms, 8 receiving connections per room | Rust Wasm / Wasmtime + direct host calls | 671 (0.98×) | 671 (0.98×) | 153.94 (1.46×) | 221.24 (1.20×) |
| 5 rooms, 8 receiving connections per room | Native Rust | 686 (1.00×) | 686 (1.00×) | 127.00 (1.21×) | 253.71 (1.38×) |


## Process startup

### Compile Wasm in each new process

Each runtime starts in 10 fresh processes from the same initial room data. The runner polls for a validated home page every 1 ms, then requests the first room's HTML page. Filesystem caches stay warm; compiled Wasm code is shared only within each process. The room response includes object activation, storage opening, and application initialization. Columns report medians independently, so the median parts need not add up to the median total. Memory is sampled after the first room response.

| Application runtime | Start → home page response (ms) | Home page → first room response (ms) | Start → first room response (ms); min–max across trials | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 257.66 (1.00×) | 30.74 (1.00×) | 288.80 (1.00×)<br>275.67–301.24 | 39.2 (1.00×) |
| Rust Wasm / V8 + direct host calls | 237.18 (0.92×) | 25.62 (0.83×) | 262.38 (0.91×)<br>259.88–273.62 | 30.8 (0.79×) |
| Rust Wasm / Wasmtime + direct host calls | 308.38 (1.20×) | 23.36 (0.76×) | 331.33 (1.15×)<br>327.76–345.39 | 30.7 (0.78×) |
| Native Rust | 237.37 (0.92×) | 23.80 (0.77×) | 261.19 (0.90×)<br>252.39–273.42 | 24.6 (0.63×) |

### Process startup and the Wasm compilation cache

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64. Medians of 10 fresh processes per row, starting from the same room data. The cached Wasmtime row loads machine code saved before timing. Filesystem caches stay warm.

| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 284.46 (1.00×) | 278.90 (1.00×) | 39.9 (1.00×) |
| Rust Wasm / Wasmtime: compile at startup | 330.20 (1.16×) | 311.40 (1.12×) | 31.1 (0.78×) |
| Rust Wasm / Wasmtime: reuse compiled module | 259.60 (0.91×) | 243.54 (0.87×) | 28.5 (0.72×) |
| Native Rust | 259.55 (0.91×) | 242.57 (0.87×) | 25.1 (0.63×) |

Wasm module compilation/loading: **66.52 ms → 1.02 ms** when reusing compiled code. All 10 cached trials hit the cache. Load tests exclude startup and compilation. [Raw data](compile-cache/cold-cache-results.json).

## Correctness checks and recorded sources

The `results.json`, `cold-results.json`, and `validation.json` records contain **180 HTTP trials, 24 WebSocket trials, and 40 startup trials**, plus **2,852 dedicated full-response checks**. The HTTP load clients completed 8,515,105 requests, including 90,791 posts. The WebSocket sessions added 7,077 posts, 123,141 verified message deliveries, and 123,141 verified HTTP read confirmations. All trials passed response and ordering checks; all 204 post-trial audits passed across all 50 room databases. Source hashes agree across these three records, and captured files match their recorded SHA-256 hashes.

The `compile-cache/` comparison contains **40 additional startup trials**. Its referenced source files match their recorded SHA-256 hashes.

The load tests started at 2026-10-15T05:01:30Z and completed at 2026-10-15T05:41:42Z. The load-test JSON records the before/after thermal checks. The largest recorded load-client CPU usage was 68.3% of one core; clients ran in separate processes on the same machine.

An independent Python implementation produces expected response bytes from the initial data and confirmed posts. Dedicated validation and warmup compare complete bodies. The HTTP load client checks every read's full-body length and CRC32, verifies write acknowledgments, and requires unique write versions with no gaps. A read must include any write confirmed before that read was sent. After each load trial the runner checks every room's current page and audits every SQLite message, timestamp, user, unread count, room version, and `PRAGMA integrity_check`.

Dedicated checks cover invalid credentials, malformed login types and membership, cache invalidation, multiple users, 50 maximum-length messages, HTML expanding beyond 1 MiB, and 8 KiB messages whose JSON encoding exceeds 32 KiB. Restart validation deletes celld's entire local runtime directory and recovers all rooms from the object store, retaining the checked messages and unread changes.

The full data are [load tests and resource use](results.json), [startup](cold-results.json), and [correctness](validation.json). They contain SHA-256 hashes of the measured binaries, guest, application, adapters, clients, and relevant sources and lockfiles. The `source_snapshot` field identifies the captured source files. The `recorded_only` map identifies hashes whose corresponding files are not distributed with the data. Build, test, measurement, and report scripts live under `bench/`. Run phases and build commands are in the [reproduction guide](../../REPRODUCE.md).

## What this benchmark covers

The direct adapters implement this application's buffered HTTP, SQL, object routing, and WebSocket needs. They are not a general implementation of every Workers API. Native Rust is a trusted-code performance reference and does not provide an untrusted-code sandbox. The Wasm adapters enforce memory ranges and object storage scope, but these runs do not include production tenant CPU interruption or prove hostile-tenant isolation. This is a single-node, local persistence study; it does not measure a fleet, remote-bucket latency, network failure, or global placement.

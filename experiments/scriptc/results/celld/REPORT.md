# scriptc on celld: full chat application

Apple M4 Pro, 48 GiB RAM, macOS arm64 · 2026-10-19. Medians of three 3-second trials; one celld Tokio thread. All ratios use JavaScript as the baseline.

## Server-rendered HTML (without in-memory page caching)

32 simultaneous clients.

| Application runtime | Requests/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/request | Peak process memory (MiB) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 2,488 (1.00×)<br>2,472–2,572 | 114.3 (1.00×) | 459.5 (1.00×) | 111.5 (1.00×) | 20.79 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 1,635 (0.66×)<br>1,611–1,668 | 106.8 (0.93×) | 655.6 (1.43×) | 64.6 (0.58×) | 26.77 (1.29×) |
| Native Rust | 10,595 (4.26×)<br>10,333–10,904 | 138.9 (1.22×) | 131.1 (0.29×) | 44.8 (0.40×) | 4.64 (0.22×) |

## Read messages as JSON (no response cache)

32 simultaneous clients.

| Application runtime | Requests/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/request | Peak process memory (MiB) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 3,050 (1.00×)<br>3,020–3,236 | 116.1 (1.00×) | 380.9 (1.00×) | 109.7 (1.00×) | 15.96 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 5,199 (1.70×)<br>4,731–5,428 | 120.3 (1.04×) | 233.0 (0.61×) | 67.0 (0.61×) | 9.27 (0.58×) |
| Native Rust | 13,224 (4.34×)<br>12,968–13,824 | 148.8 (1.28×) | 112.2 (0.29×) | 46.2 (0.42×) | 4.16 (0.26×) |

## Server-rendered HTML (with in-memory page caching)

32 simultaneous clients.

| Application runtime | Requests/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/request | Peak process memory (MiB) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 4,801 (1.00×)<br>4,499–4,967 | 124.5 (1.00×) | 259.8 (1.00×) | 120.4 (1.00×) | 9.19 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 14,088 (2.93×)<br>13,721–14,343 | 153.3 (1.23×) | 108.8 (0.42×) | 91.1 (0.76×) | 3.84 (0.42×) |
| Native Rust | 26,155 (5.45×)<br>25,970–27,965 | 180.0 (1.45×) | 68.8 (0.26×) | 62.4 (0.52×) | 1.95 (0.21×) |

## Post a message; reply after saving it

32 simultaneous clients.

| Application runtime | Requests/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/request | Peak process memory (MiB) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 430 (1.00×)<br>423–469 | 72.2 (1.00×) | 1705.3 (1.00×) | 113.8 (1.00×) | 764.87 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 380 (0.88×)<br>346–388 | 61.4 (0.85×) | 1583.5 (0.93×) | 81.4 (0.72×) | 804.01 (1.05×) |
| Native Rust | 434 (1.01×)<br>409–485 | 61.8 (0.86×) | 1469.7 (0.86×) | 60.2 (0.53×) | 598.48 (0.78×) |

## Read message history while posting new messages

32 reading clients and one client attempting 100 posts/s; throughput includes reads and posts.

| Application runtime | Requests/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/request | Peak process memory (MiB) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 4,194 (1.00×)<br>3,315–4,342 | 134.5 (1.00×) | 324.9 (1.00×) | 146.3 (1.00×) | 15.82 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 10,011 (2.39×)<br>9,617–11,827 | 150.6 (1.12×) | 146.7 (0.45×) | 132.9 (0.91×) | 9.29 (0.59×) |
| Native Rust | 24,088 (5.74×)<br>23,533–26,153 | 189.6 (1.41×) | 78.3 (0.24×) | 75.7 (0.52×) | 7.52 (0.48×) |

| Application runtime | Reads/s | Posts/s | Reads served from page cache (%) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 | 4,132 | 62.2 | 94.1 |
| TypeScript → scriptc Wasm / Wasmtime | 9,920 | 94.9 | 96.3 |
| Native Rust | 23,990 | 99.9 | 98.4 |

The posting client can fall below its 100 posts/s target. The achieved write rates above describe the load each mode completed.

## WebSocket chat: 1 room, 32 receiving connections

One sender per room waits for persistence, broadcast, and every participant's HTTP read confirmation before sending the next message.

| Application runtime | Messages/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/message | Peak process memory (MiB) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 42 (1.00×)<br>41–43 | 180.3 (1.00×) | 42974.0 (1.00×) | 117.9 (1.00×) | 35.66 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 44 (1.04×)<br>44–44 | 173.6 (0.96×) | 39134.2 (0.91×) | 85.1 (0.72×) | 33.77 (0.95×) |
| Native Rust | 45 (1.06×)<br>42–45 | 179.1 (0.99×) | 39579.1 (0.92×) | 61.9 (0.53×) | 33.69 (0.94×) |

## WebSocket chat: 5 rooms, 8 receiving connections per room

One sender per room waits for persistence, broadcast, and every participant's HTTP read confirmation before sending the next message.

| Application runtime | Messages/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/message | Peak process memory (MiB) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| JavaScript / V8 | 91 (1.00×)<br>90–92 | 110.2 (1.00×) | 12059.3 (1.00×) | 113.8 (1.00×) | 150.71 (1.00×) |
| TypeScript → scriptc Wasm / Wasmtime | 86 (0.94×)<br>76–88 | 97.6 (0.89×) | 11306.5 (0.94×) | 81.5 (0.72×) | 197.45 (1.31×) |
| Native Rust | 87 (0.95×)<br>87–87 | 87.6 (0.79×) | 10122.4 (0.84×) | 58.1 (0.51×) | 224.14 (1.49×) |

CPU and physical memory cover the entire celld process. Each trial includes 2,000 warmup reads before timing and a subsequent audit of all 50 room databases. Module compilation and startup occur before load timing.

All 63 load trials and 2,460 dedicated HTTP response checks pass. Validation also checks ordered WebSocket broadcasts, unread counts, and recovery from the object store after deleting the runtime directory. Each mode returns HTML and JSON after receiving 50 messages of 8,192 NUL bytes each, including page-cache behavior when JSON escaping exceeds the 1 MiB page-cache limit.

[Load measurements](results.json) · [Validation](validation.json) · [Measured source snapshots](sources/)

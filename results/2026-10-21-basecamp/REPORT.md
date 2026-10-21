# Original Basecamp Rust application

Unmodified [once-campfire-rust](https://github.com/basecamp/once-campfire-rust/tree/6dae2fd3c1a2dce61ca11beb731bcce2b42b67ad) at `6dae2fd3c1a2dce61ca11beb731bcce2b42b67ad`. Apple M4 Pro, 48 GiB RAM, macOS-26.5.2-arm64-arm-64bit-Mach-O · 2026-10-21. Native Rust 1.98.1, release profile with the upstream fat LTO settings.

The process serves its original HTTP front end, Axum controllers, Askama views, authentication, rich text, and SQLite database. The fixture contains 50 rooms, 1,000 messages per room, and the same message text and user names as the Workers chat. All timed requests use 32 clients, the same room/user rotation, HTTP/1.1, and identity encoding.

## Results

Medians of 3 5-second trials. Each trial restores the seed and performs 2,000 warmup reads; posts also warm up with 1,000 saved messages.

Ratios use the corresponding uncached read, cached read, or post from workerd JS in the same session. The full room page has no matching Workers workload.

| Original application operation | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/request (µs) | Peak process memory (MiB) | p99 response time (ms) | Response size (KiB) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Messages HTML; response and fragment caches disabled | 277 (0.06×) | 99.8 (0.98×) | 3,607.3 (15.24×) | 45.6 (0.26×) | 183.33 (14.96×) | 385.0 |
| Messages HTML; original response and fragment caches enabled | 13,391 (1.23×) | 99.6 (0.98×) | 74.4 (0.80×) | 117.8 (0.35×) | 4.07 (0.30×) | 385.0 |
| Full room page; response and fragment caches disabled | 273 | 99.8 | 3,652.9 | 57.8 | 180.43 | 417.4 |
| Post a message; original caches enabled | 3,099 (0.49×) | 137.1 (1.49×) | 442.8 (3.04×) | 133.2 (0.57×) | 19.39 (0.71×) | 9.8 |

The three workerd modes execute the same Workers-style application and can be compared with one another. The original Basecamp application does different work: 40 richly marked-up messages per page, one SQLite database, cookie authentication, different SQL, and its own cache policies. The Workers chat returns 50 plain-text messages and stores one SQLite database per room. Original writes use SQLite WAL with synchronous=NORMAL; local workerd uses its own SQLite/VFS and automatic transactions. These ratios compare whole applications and do not isolate the overhead of the Workers/DO design.

## Startup

Medians of 10 fresh processes with the original caches enabled. The wall time runs from spawning the executable through a validated /up health response to receipt of the first authenticated 40-message HTML response; response-content validation runs after timing. Filesystem caches stay warm.

| Start → first messages response (ms) | CPU time to first response (ms) | Process memory after first response (MiB) |
| ---: | ---: | ---: |
| 27.51 | 22.93 | 15.5 |

## Validation and runtime settings

The functional suite validates 814 responses across disabled/default-cache configurations, authenticated pages, rejection of unauthenticated reads, post invalidation, and persistence after restart. Every measured read matches the full bytes of a preflight page whose message IDs and text are independently checked against the fixture. Every acknowledged POST is checked in the message, rich-text, and full-text-search tables after shutdown. All load trials have zero errors and pass SQLite integrity and foreign-key checks.

The original executable runs with TOKIO_WORKER_THREADS=1, RAILS_MAX_THREADS=1, and JOB_CONCURRENCY=1. Its database, checkpoint, jobs, and network helper threads remain part of the process. CPU covers every thread; memory is the macOS process footprint sampled every 100 ms. Uncached cases set CAMPFIRE_RESPONSE_CACHE_MB=0 and CAMPFIRE_FRAGMENT_CACHE_MB=0. Cached and POST cases use the original 64 MiB response and 32 MiB fragment limits.

The executable links libvips 8.16.1 and GLib 2.88.1; its feature configuration is recorded in the [build manifest](sources/.build/basecamp-host/build.json). This text workload does not call media handlers or initialize libvips.

The workerd comparison has the same cohort identifier `2026-10-21T05:52:59Z`. For matching read/post cases the four executables run sequentially with their order rotated by round. Workerd-only JSON, mixed, and WebSocket cases and the original full-room page run in the same session.

[Original load data](results.json) · [Startup data](cold-results.json) · [Functional validation](validation.json) · [Source snapshots](sources/) · [workerd report](../2026-10-21-workerd/REPORT.md) · [Reproduce](../../experiments/basecamp/README.md)

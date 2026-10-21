# workerd chat benchmark

Compare three request paths inside [workerd](https://github.com/cloudflare/workerd/tree/781048bc05cb205c92e503c1062ef3266ab959b5), using the same chat application, release executable, database seed, load generators, and resource sampler.

| Mode | Application execution | Bindings |
| --- | --- | --- |
| `js` | Standard JS Worker and Durable Object handlers in V8 | Standard JS APIs |
| `wasm-js` | Standard JS entry → Rust Wasm in V8 | Wasm → JS callbacks → workerd APIs; the standard Wasm Worker model |
| `wasm-v8` | Patched native entry → the same Rust Wasm in V8 | Wasm → C++ callbacks → workerd's native implementations |

The direct mode keeps workerd's actual object routing, activation, request scheduling, SQLite storage, write/output gates, and WebSocket manager. SQLite executes outside Wasm in every mode. The benchmark compares complete paths, including request/response conversion, SQL row transfer, and per-socket sends.

[`native-worker.js`](native-worker.js) supplies module exports at startup; every application callback throws. Direct mode therefore fails if a request reaches the JS constructor, HTTP handler, or WebSocket handler. V8's native callable ABI and Wasm-to-host wrappers remain. The prototype implements this application's operations; full asynchronous Workers API compatibility, tracing, cancellation, and tenant CPU limits are outside its scope.

## Build

Resource sampling requires macOS. Install Git, Xcode Command Line Tools, Python 3.11+, Rustup, and [Bazelisk](https://github.com/bazelbuild/bazelisk). Workerd's source selects its Bazel version; the recorded build uses Bazel 9.3.0. The first build compiles V8 and needs substantial disk space and memory.

From the repository root:

```sh
python3 experiments/workerd/build_workerd.py
```

The builder fetches the exact revision in [`workerd.json`](workerd.json), applies [`patches/workerd.patch`](../../patches/workerd.patch), copies the native Wasm host files into workerd, and builds with `--config=release --jobs=8`. It also builds the shared Rust Wasm module and load clients with locked dependencies. The executable is `.build/workerd-host/workerd`; the build manifest records source and binary hashes.

## Run

Port **19880** must be free. Run the three modes sequentially on an otherwise idle machine:

```sh
python3 experiments/workerd/run.py all
python3 experiments/workerd/report.py .runs/workerd > .runs/workerd/REPORT.md
python3 experiments/workerd/report.py .runs/workerd --check
```

`all` prepares one common seed, validates all three modes, runs three 5-second trials per workload with 32 HTTP clients, and measures ten fresh processes per mode. Mode order rotates between rounds. Workloads cover uncached HTML and JSON, cached HTML, posts, mixed reads/posts, and WebSocket rooms with HTTP read confirmations. Every trial restores the seed and ends with an audit of all 50 SQLite databases.

To run an individual phase:

```sh
python3 experiments/workerd/run.py prepare
python3 experiments/workerd/run.py validate
python3 experiments/workerd/run.py bench --seconds 5 --rounds 3
python3 experiments/workerd/run.py cold --cold-rounds 10
```

Measurements, logs, generated configurations, seed hashes, and measured source snapshots live under `.runs/workerd/`. The runner checks the build manifest and stops its own server processes. Fresh-process measurements include Wasm compilation; no compiled-module cache is configured. Filesystem caches stay warm.

To include Basecamp's unmodified Rust application in the same session, use the [original application comparison](../basecamp/README.md#run-the-comparison).

## Verify the recorded results

Python 3.11+ is sufficient to verify the published tables and source snapshots on any operating system:

```sh
python3 experiments/workerd/report.py --check
```

The [recorded report](../../results/2026-10-21-workerd/REPORT.md) is a separate workerd cohort. Its ratios use workerd JS as the baseline. Workerd uses its local SQLite backend; the celld cohort uses celld's LTX persistence. Neither cohort measures Cloudflare's hosted service.

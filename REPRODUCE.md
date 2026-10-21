# Build and run the benchmark

For workerd and the original Basecamp Rust application, see [the workerd experiment](experiments/workerd/README.md) and [the original application comparison](experiments/basecamp/README.md).

## Requirements

The resource measurements require **macOS**, Git, Xcode Command Line Tools, Python 3.11+, Rustup, and [Vite+](https://viteplus.dev/). The recorded runs use Apple Silicon. [`rust-toolchain.toml`](rust-toolchain.toml) selects Rust 1.96.0 and the Wasm target.

## Build

Install the [pinned scriptc and Zig tools](experiments/scriptc/REPRODUCE.md#install-the-pinned-tools), then run from the repository root:

```sh
vp install --frozen-lockfile
python3 experiments/scriptc/celld/build.py
```

The build installs the Rust toolchain and Wasm target, fetches the exact celld revision in [`celld.json`](celld.json), applies [`patches/celld.patch`](patches/celld.patch), and builds celld, the Rust Wasm module, and both load generators with locked dependencies. Generated source and binaries live under `.build/`. A generated checkout with a different patch is preserved under `.build/celld-backup-*/`.

All six modes use the same celld executable. The three Rust Wasm modes use the same module. `wasm-js` imports it through celld's standard Worker deployment loader; the direct Wasm modes load it from Rust. The build also compiles the TypeScript chat handlers to a separate Wasm module with scriptc 0.3.6.

## Run the suite

This matches the recorded 32-client comparison:

```sh
python3 bench/run.py --phase all --seconds 5 --rounds 3 --concurrencies 32
python3 bench/report.py .runs/current > .runs/current/REPORT.md
python3 bench/report.py .runs/current --check
```

The suite takes tens of minutes in addition to build time. Ports **19878** and **19879** must be free. The six runtimes run one at a time, with their order rotated between rounds. The runner stops the processes it launches. Logs, database snapshots, measurements, and copies of the measured sources live under `.runs/current/`.

For additional client counts, use `--concurrencies 1,32,128`.

## Run individual phases

```sh
python3 bench/run.py --phase prepare
python3 bench/run.py --phase validate
python3 bench/run.py --phase bench --seconds 5 --rounds 3 --concurrencies 32
python3 bench/run.py --phase cold
```

`prepare` creates JS and JS-to-Wasm deployment snapshots with matching data: 50 rooms, each with 1,000 messages. `validate` checks complete response bytes, permissions, cache invalidation, large and escaped messages, WebSocket ordering, unread counts, and recovery after deleting the local runtime directory. Each load trial restores its deployment snapshot and ends by checking all 50 room databases. `cold` measures ten fresh processes per runtime. Every phase checks build-input and binary hashes before running.

## Compare the Wasm compilation cache

This runs as part of `all`, or separately after building:

```sh
python3 bench/run.py --phase cold-cache
python3 bench/report.py .runs/current/compile-cache > .runs/current/compile-cache/REPORT.md
python3 bench/report.py .runs/current/compile-cache --check
```

`cold-cache` creates its own deployment snapshots and a Wasmtime compiled-code cache under `.runs/current/compile-cache/`. It saves compiled machine code before timing, verifies cache hits, and measures ten fresh processes per configuration. Filesystem caches stay warm.

## Verify the published results

These checks require only Python 3.11+ and work on other operating systems:

```sh
python3 bench/report.py --check
python3 bench/report.py results/2026-10-19/compile-cache --check
```

To regenerate the published reports:

```sh
python3 bench/report.py > results/2026-10-19/REPORT.md
python3 bench/report.py results/2026-10-19/compile-cache > results/2026-10-19/compile-cache/REPORT.md
```

## Open the chat app

After building, start the JavaScript implementation:

```sh
CELLD_ESBUILD="$PWD/node_modules/.bin/esbuild" \
WORKERS_CHAT_ENGINE=js \
WORKERS_CHAT_WASM="$PWD/.build/guest-target/wasm32-unknown-unknown/release/workers_chat_guest.wasm" \
.build/host-target/release/celld dev application --port 19878 --no-watch
```

Open `http://127.0.0.1:19878/`. Demo users are 1 through 7; user N has password `password-N`. Select `wasm`, `native`, or `wasm-v8` through `WORKERS_CHAT_ENGINE` for a direct Rust-hosted implementation. Stop the process before switching.

To open the Wasm implementation with JS bindings, create a separate local deployment:

```sh
mkdir -p .build/wasm-js-app
cp application/wrangler.jsonc .build/wasm-js-app/
cp application/wasm-worker.js .build/wasm-js-app/worker.js
cp .build/guest-target/wasm32-unknown-unknown/release/workers_chat_guest.wasm .build/wasm-js-app/chat.wasm
CELLD_ESBUILD="$PWD/node_modules/.bin/esbuild" \
WORKERS_CHAT_ENGINE=wasm-js \
.build/host-target/release/celld dev .build/wasm-js-app --port 19878 --no-watch
```

The browser app commands use celld's development configuration. The benchmark runner sets the measured thread count, client count, and runtime configuration.

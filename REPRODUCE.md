# Reproduce the benchmarks

The resource runner requires **macOS**, Git, Xcode Command Line Tools, Python 3.11+, Rustup, and [Vite+](https://viteplus.dev/). `rust-toolchain.toml` selects Rust 1.96.0 and the Wasm target. The recorded results come from Apple Silicon. Reading and verifying the recorded reports needs only Python 3.11+ and works on other operating systems.

From the repository root:

```sh
vp install --frozen-lockfile
python3 bench/build.py
python3 bench/run.py --phase all --seconds 5 --rounds 3 --concurrencies 1,32,128
python3 bench/report.py .runs/current > .runs/current/REPORT.md
python3 bench/report.py .runs/current --check
```

The build installs the pinned Rust toolchain and Wasm target through Rustup, fetches the exact celld revision in [`celld.json`](celld.json), applies [`patches/celld.patch`](patches/celld.patch), copies the application and adapter into that generated checkout, and builds the host, Wasm module, and two pressure clients with locked dependencies. Generated source and build outputs live under `.build/`. The patch adds the application's direct Rust/Wasm execution path to celld. All modes use the same patched executable; JS executes through celld's existing Workers APIs.

If the generated checkout uses a different patch, the build preserves it under `.build/celld-backup-*/` and fetches a fresh checkout.

The full suite takes tens of minutes on the recorded machine, in addition to build time. It runs the four modes sequentially, rotating their order between rounds. Ports **19878** and **19879** must be free. The runner owns and stops the processes it launches. Logs, seed snapshots, disposable runtime data, fresh measurement JSON, and captured source files live under `.runs/current/`; recorded results live under `results/2026-10-15/`.

The phases can also run separately:

```sh
python3 bench/run.py --phase prepare
python3 bench/run.py --phase validate
python3 bench/run.py --phase bench --seconds 5 --rounds 3 --concurrencies 1,32,128
python3 bench/run.py --phase cold
```

`prepare` creates and audits the persistent seed. `validate` checks full response bytes, invalid input and permissions, cache invalidation, large/escaped messages, ordered WebSocket broadcasts, unread changes, and restart recovery after deleting the local runtime directory. Every pressure trial restores the same seed and ends with an audit of all 50 room databases. `cold` measures ten fresh processes per engine. Build-input and binary hashes are checked before running a phase.

The compilation-cache comparison runs as part of `all`, or independently after building:

```sh
python3 bench/run.py --phase cold-cache
python3 bench/report.py .runs/current/compile-cache > .runs/current/compile-cache/REPORT.md
python3 bench/report.py .runs/current/compile-cache --check
```

`cold-cache` prepares its own seed and isolated Wasmtime filesystem cache under `.runs/current/compile-cache/`. It populates the cache in one process, then measures ten fresh processes each for JS, Wasmtime with caching disabled, Wasmtime with a verified cache hit, and native Rust. Engine order rotates between rounds. Every process validates the root and first room response. The JSON records CPU time, physical memory, Wasmtime compilation/loading time, cache-hit counters, and source and binary hashes.

To check the published tables and captured source hashes, with no Rust build or package installation:

```sh
python3 bench/report.py --check
```

To regenerate the complete recorded report:

```sh
python3 bench/report.py > results/2026-10-15/REPORT.md
```

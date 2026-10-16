# Wasmtime compilation-cache startup comparison

### Process startup and the Wasm compilation cache

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64. Medians of 10 fresh processes per row, starting from the same room data. The cached Wasmtime row loads machine code saved before timing. Filesystem caches stay warm.

| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 289.96 (1.00×) | 265.26 (1.00×) | 39.5 (1.00×) |
| Rust Wasm / V8 + JS bindings | 301.52 (1.04×) | 285.15 (1.07×) | 42.3 (1.07×) |
| Rust Wasm / V8 + direct host calls | 271.82 (0.94×) | 245.77 (0.93×) | 30.9 (0.78×) |
| Rust Wasm / Wasmtime: compile at startup | 324.56 (1.12×) | 326.18 (1.23×) | 32.1 (0.81×) |
| Rust Wasm / Wasmtime: reuse compiled module | 269.17 (0.93×) | 247.56 (0.93×) | 28.3 (0.71×) |
| Native Rust | 253.20 (0.87×) | 240.01 (0.90×) | 24.8 (0.63×) |

Wasm module compilation/loading: **66.21 ms → 1.03 ms** when reusing compiled code. All 10 cached trials hit the cache. Load tests exclude startup and compilation. [Raw data](cold-cache-results.json).

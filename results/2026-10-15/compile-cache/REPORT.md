# Wasmtime compilation-cache startup comparison

### Process startup and the Wasm compilation cache

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64. Medians of 10 fresh processes per row, starting from the same room data. The cached Wasmtime row loads machine code saved before timing. Filesystem caches stay warm.

| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 284.46 (1.00×) | 278.90 (1.00×) | 39.9 (1.00×) |
| Rust Wasm / Wasmtime: compile at startup | 330.20 (1.16×) | 311.40 (1.12×) | 31.1 (0.78×) |
| Rust Wasm / Wasmtime: reuse compiled module | 259.60 (0.91×) | 243.54 (0.87×) | 28.5 (0.72×) |
| Native Rust | 259.55 (0.91×) | 242.57 (0.87×) | 25.1 (0.63×) |

Wasm module compilation/loading: **66.52 ms → 1.02 ms** when reusing compiled code. All 10 cached trials hit the cache. Load tests exclude startup and compilation. [Raw data](cold-cache-results.json).

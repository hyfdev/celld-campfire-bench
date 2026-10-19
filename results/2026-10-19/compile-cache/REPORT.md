# Wasmtime compilation-cache startup comparison

### Process startup and the Wasm compilation cache

Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64. Medians of 10 fresh processes per row, starting from the same room data. The cached Wasmtime row loads machine code saved before timing. Filesystem caches stay warm.

| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 290.72 (1.00×) | 271.70 (1.00×) | 39.4 (1.00×) |
| Rust Wasm / V8 + JS bindings | 308.10 (1.06×) | 287.06 (1.06×) | 42.2 (1.07×) |
| Rust Wasm / V8 + direct host calls | 268.21 (0.92×) | 244.77 (0.90×) | 31.0 (0.79×) |
| Rust Wasm / Wasmtime: compile at startup | 334.06 (1.15×) | 306.42 (1.13×) | 31.2 (0.79×) |
| Rust Wasm / Wasmtime: reuse compiled module | 269.19 (0.93×) | 241.32 (0.89×) | 28.1 (0.71×) |
| TypeScript → scriptc Wasm / Wasmtime: compile at startup | 442.62 (1.52×) | 406.71 (1.50×) | 42.9 (1.09×) |
| Native Rust | 270.50 (0.93×) | 240.40 (0.88×) | 25.1 (0.64×) |

Wasm module compilation/loading: **69.14 ms → 1.05 ms** when reusing compiled code. All 10 cached trials hit the cache. Load tests exclude startup and compilation. [Raw data](cold-cache-results.json).

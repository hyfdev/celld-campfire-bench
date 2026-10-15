# Wasmtime compilation-cache startup comparison

### Process startup with a compilation cache

Apple M4 Pro, 48 GiB RAM, `macOS-26.5.2-arm64-arm-64bit-Mach-O`. Medians of 10 fresh processes per configuration. The executable and seed are shared; both Wasm modes use the same module. Wasmtime's compilation cache is populated before timing; filesystem caches stay warm.

| Application execution | Launch → first room page, ms | CPU time through first page, ms | Process memory after page, MiB |
| --- | ---: | ---: | ---: |
| JavaScript / V8 (baseline) | 284.46 (1.00×) | 278.90 (1.00×) | 39.9 (1.00×) |
| Rust Wasm / Wasmtime: cache disabled | 330.20 (1.16×) | 311.40 (1.12×) | 31.1 (0.78×) |
| Rust Wasm / Wasmtime: cache hit | 259.60 (0.91×) | 243.54 (0.87×) | 28.5 (0.72×) |
| Native Rust | 259.55 (0.91×) | 242.57 (0.87×) | 25.1 (0.63×) |

Wasm module compile/load: **66.52 ms → 1.02 ms** on a cache hit. All 10 cached trials hit the cache. Throughput tests exclude compilation through warmup. [Raw data](cold-cache-results.json).

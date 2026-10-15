# Wasmtime compilation-cache startup comparison

### Process startup with a compilation cache

Separate matched run on Apple M4 Pro, 48 GiB RAM, `macOS-26.5.2-arm64-arm-64bit-Mach-O`: 10 fresh processes per configuration, with the same celld executable and seeded database snapshot. Both Wasm configurations use the same module. The cached configuration uses Wasmtime 49's built-in filesystem compilation cache, populated in a separate process before timing. Filesystem caches remain warm.

The table shows medians as **absolute value (ratio to JS)**, with JS at **1.00×**. Lower values are better. CPU is cumulative server user + system time through the first room response; memory is the process physical footprint immediately after that response.

| Application execution | Process launch → first room page, ms | Server CPU, ms | Process memory after first page, MiB |
| --- | ---: | ---: | ---: |
| JavaScript / V8 | 284.46 (1.00×) | 278.90 (1.00×) | 39.9 (1.00×) |
| Rust Wasm / Wasmtime: cache disabled | 330.20 (1.16×) | 311.40 (1.12×) | 31.1 (0.78×) |
| Rust Wasm / Wasmtime: cache hit | 259.60 (0.91×) | 243.54 (0.87×) | 28.5 (0.72×) |
| Native Rust | 259.55 (0.91×) | 242.57 (0.87×) | 25.1 (0.63×) |

Every cached process records one cache hit and zero misses; the cache-populating process records one miss. Wasm module compilation/loading (`Module::new`) takes 66.52 ms with caching disabled and 1.02 ms on a cache hit. The full startup table also includes service initialization, object activation, and the first page response.

The cache stores compiled Wasm code. Application execution, SQL calls, and page caching use the same paths in both Wasm configurations. The throughput tests start after warmup, which already excludes module compilation. [Startup measurements and source hashes](cold-cache-results.json).

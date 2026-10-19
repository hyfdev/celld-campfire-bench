# Build and run scriptc on celld

The recorded comparison requires Apple Silicon macOS, Python 3.11+, Xcode Command Line Tools, Rustup, and Vite+. The whole-process resource sampler uses macOS APIs. No Node process serves the compiled TypeScript application.

## Install the pinned tools

From the repository root:

```sh
vp install --frozen-lockfile
mkdir -p experiments/scriptc/.build/tools/scriptc experiments/scriptc/.build/tools/runtime-wasm32-wasi experiments/scriptc/.build/tools/zig
cd experiments/scriptc/.build/tools
curl -fL https://github.com/vercel-labs/scriptc/releases/download/v0.3.6/scriptc-0.3.6-darwin-arm64.tar.gz -o scriptc-0.3.6-darwin-arm64.tar.gz
curl -fL https://registry.npmjs.org/@scriptc/runtime-wasm32-wasi/-/runtime-wasm32-wasi-0.3.6.tgz -o scriptc-runtime-wasm32-wasi-0.3.6.tgz
curl -fL https://ziglang.org/download/0.16.0/zig-aarch64-macos-0.16.0.tar.xz -o zig.tar.xz
```

Verify the downloaded archives against [`results/toolchain.json`](results/toolchain.json):

```sh
python3 - <<'PY'
import hashlib, json
from pathlib import Path
lock = json.loads(Path('../../results/toolchain.json').read_text())
for name, expected in [('scriptc-0.3.6-darwin-arm64.tar.gz', lock['scriptc_archive_sha256']),
                       ('scriptc-runtime-wasm32-wasi-0.3.6.tgz', lock['runtime_archive_sha256']),
                       ('zig.tar.xz', lock['zig']['shasum'])]:
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name
PY
tar -xzf scriptc-0.3.6-darwin-arm64.tar.gz -C scriptc
tar -xzf scriptc-runtime-wasm32-wasi-0.3.6.tgz -C runtime-wasm32-wasi
tar -xJf zig.tar.xz -C zig
cd ../../../..
```

## Build and measure

From the repository root:

```sh
python3 experiments/scriptc/celld/build.py
python3 experiments/scriptc/celld/run.py
python3 experiments/scriptc/celld/report.py > experiments/scriptc/results/celld/REPORT.md
python3 experiments/scriptc/celld/report.py --check
```

The build generates TypeScript constants from the shared SQL, schema, and browser UI, compiles the TypeScript application to Wasm using scriptc's default `release` optimization, and builds the extended celld server and load clients. `scriptc` 0.3.6 uses LLVM and links its C implementation of strings, JSON, regular expressions, and memory management. No compiler or runtime source patches are applied. Wasmtime 49 is linked into celld by the existing benchmark build; no Wasmtime CLI is needed.

Ports 19878 and 19879 must be free. The runner creates matching data for 50 rooms with 1,000 messages each, validates all three modes, and runs three 3-second trials of each HTTP and WebSocket workload. Modes run sequentially and their order rotates between rounds. Warmup, startup, and Wasm compilation precede timing. The runner stops every process it launches.

Use `--phase validate` or `--phase bench` to run either phase separately. Build-input and binary hashes are checked before either phase. Generated libraries, compiler caches, databases, and trial logs stay under `.build/`; measurements and source snapshots are copied to `results/celld/`.

## Run the chat application

After building, from the repository root:

```sh
CELLD_ESBUILD="$PWD/node_modules/.bin/esbuild" \
CELLD_TOKIO_THREADS=1 \
WORKERS_CHAT_ENGINE=scriptc-wasm \
WORKERS_CHAT_TS_WASM="$PWD/experiments/scriptc/.build/chat.wasm" \
.build/host-target/release/celld dev application --port 19878 --no-watch
```

Open `http://127.0.0.1:19878/`. User N has password `password-N` for users 1–7.

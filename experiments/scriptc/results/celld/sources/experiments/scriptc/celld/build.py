import hashlib
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BUILD = HERE.parent / ".build"
TOOLS = BUILD / "tools"
COMPILER = TOOLS / "scriptc/bin/scriptc"
RUNTIME = TOOLS / "runtime-wasm32-wasi/package"
ZIG = TOOLS / "zig/zig-aarch64-macos-0.16.0"
MANIFEST = BUILD / "celld-build.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, env=None):
    subprocess.run([str(part) for part in command], cwd=HERE, env=env, check=True)


def main():
    data = ""
    for name, path in (("SQL", ROOT / "app/sql.json"), ("SCHEMA", ROOT / "app/schema.json")):
        data += f"export const {name} = {json.dumps(json.loads(path.read_text()), ensure_ascii=False, indent=2)};\n"
    data += f"export const UI = {json.dumps((ROOT / 'application/index.html').read_text(), ensure_ascii=False)};\n"
    (HERE / "data.ts").write_text(data)
    BUILD.mkdir(parents=True, exist_ok=True)
    wasm_env = dict(os.environ, PATH=str(ZIG) + os.pathsep + os.environ["PATH"],
                    SCRIPTC_TARGET="wasm32-wasi", SCRIPTC_RUNTIME_PACK=str(RUNTIME))
    run([COMPILER, "build", "--lib", "--profile", "wasm.json", "-o", BUILD / "chat.wasm"], wasm_env)
    run([sys.executable, ROOT / "bench/build.py"])
    inputs = [HERE / name for name in ("app.ts", "data.ts", "wasm.json", "build.py", "run.py")]
    inputs += [ROOT / name for name in ("app/sql.json", "app/schema.json", "application/index.html")]
    manifest = {
        "compiler": subprocess.check_output([str(COMPILER), "--version"], text=True).strip(),
        "toolchain": json.loads((HERE.parent / "results/toolchain.json").read_text()),
        "compiler_sha256": digest(COMPILER),
        "zig_sha256": digest(ZIG / "zig"),
        "runtime_pack_sha256": digest(RUNTIME / "runtime-pack.json"),
        "inputs": {str(path.relative_to(ROOT)): digest(path) for path in inputs},
        "binaries": {str(path.relative_to(ROOT)): digest(path) for path in (BUILD / "chat.wasm",)},
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()

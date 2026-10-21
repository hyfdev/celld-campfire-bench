import hashlib
import json
import pathlib
import shutil
import subprocess
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = ROOT / "experiments/workerd"
SOURCE = ROOT / ".build/workerd-source"
OUTPUT = ROOT / ".build/workerd-host"
BINARY = OUTPUT / "workerd"
GUEST = ROOT / ".build/guest-target/wasm32-unknown-unknown/release/workers_chat_guest.wasm"
LOAD = ROOT / ".build/load-target/release/workers-chat-load"
WS = ROOT / ".build/load-target/release/workers-chat-ws"
BINARIES = [BINARY, GUEST, LOAD, WS]
HOOKS = ["src/workerd/io/BUILD.bazel", "src/workerd/io/worker.h",
         "src/workerd/io/worker.c++", "src/workerd/io/worker-entrypoint.c++",
         "src/workerd/api/actor.h", "src/workerd/api/actor.c++",
         "src/workerd/api/hibernatable-web-socket.c++"]


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def inputs():
    paths = [ROOT / "patches/workerd.patch", ROOT / "rust-toolchain.toml", ROOT / "experiments/basecamp/load.rs"]
    for directory in ("app", "application", "guest", "load", "experiments/workerd"):
        paths += [p for p in (ROOT / directory).rglob("*") if p.is_file()
                  and not any(part in {"target", "__pycache__", ".celld", ".wrangler"} for part in p.relative_to(ROOT / directory).parts)
                  and p.suffix not in {".md"}]
    paths += [SOURCE / p for p in HOOKS + ["src/workerd/io/native-wasm.h", "src/workerd/io/native-wasm.c++"]]
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(set(paths))}


def run(command, cwd=ROOT):
    print(" ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, check=True)


def check_build():
    manifest = json.loads((OUTPUT / "build.json").read_text())
    if manifest["inputs"] != inputs():
        raise RuntimeError("workerd inputs changed; run python3 experiments/workerd/build_workerd.py")
    for path, value in manifest["binaries"].items():
        if digest(ROOT / path) != value:
            raise RuntimeError(f"Measured binary changed: {path}")
    return manifest


def main():
    lock = json.loads((HERE / "workerd.json").read_text())
    if not SOURCE.exists():
        SOURCE.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet", SOURCE])
        run(["git", "remote", "add", "origin", lock["repository"]], SOURCE)
        run(["git", "fetch", "--depth", "1", "origin", lock["revision"]], SOURCE)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], SOURCE)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    if revision != lock["revision"]:
        raise RuntimeError(f"Expected workerd {lock['revision']}, found {revision}")
    patch = ROOT / "patches/workerd.patch"
    clean = subprocess.run(["git", "apply", "--check", str(patch)], cwd=SOURCE, capture_output=True)
    if clean.returncode == 0:
        run(["git", "apply", patch], SOURCE)
    else:
        subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=SOURCE, check=True)
    for name in ("native-wasm.h", "native-wasm.c++"):
        shutil.copy2(HERE / name, SOURCE / "src/workerd/io" / name)
    bazel = shutil.which("bazelisk")
    if bazel is None:
        raise RuntimeError("Install Bazelisk before building workerd: https://github.com/bazelbuild/bazelisk")
    startup = ["--output_user_root=" + str(ROOT / ".build/workerd-bazel")]
    flags = ["--config=release", "--jobs=8"]
    command = [bazel, *startup, "build", "//src/workerd/server:workerd", *flags]
    run(command, SOURCE)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT / "workerd.tmp"
    temporary.unlink(missing_ok=True)
    shutil.copy2(SOURCE / "bazel-bin/src/workerd/server/workerd", temporary)
    temporary.replace(BINARY)
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    run(["rustup", "toolchain", "install", toolchain, "--profile", "minimal"])
    run(["rustup", "target", "add", "--toolchain", toolchain, "wasm32-unknown-unknown"])
    cargo = ["cargo", f"+{toolchain}", "build", "--locked", "--release"]
    run(cargo + ["--target", "wasm32-unknown-unknown", "--target-dir", ROOT / ".build/guest-target"], ROOT / "guest")
    run(cargo + ["--target-dir", ROOT / ".build/load-target"], ROOT / "load")
    manifest = {"workerd": lock, "build_flags": flags,
                "bazel": subprocess.check_output([bazel, *startup, "version", "--gnu_format"], cwd=SOURCE, text=True).strip(),
                "version": subprocess.check_output([BINARY, "--version"], text=True).strip(),
                "rustc": subprocess.check_output(["rustc", f"+{toolchain}", "--version"], text=True).strip(),
                "inputs": inputs(), "binaries": {str(p.relative_to(ROOT)): digest(p) for p in BINARIES}}
    (OUTPUT / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Built workerd and the shared Wasm application and load clients.", flush=True)


if __name__ == "__main__":
    main()

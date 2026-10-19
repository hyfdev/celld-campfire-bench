import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build"
CELLD_SOURCE = BUILD / "celld"
CELLD = BUILD / "host-target/release/celld"
GUEST = BUILD / "guest-target/wasm32-unknown-unknown/release/workers_chat_guest.wasm"
LOAD = BUILD / "load-target/release/workers-chat-load"
WS = BUILD / "load-target/release/workers-chat-ws"
BINARIES = [CELLD, GUEST, LOAD, WS]


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def inputs():
    paths = [ROOT / name for name in (
        "celld.json", "patches/celld.patch", "rust-toolchain.toml",
        "package.json", "pnpm-lock.yaml", "bench/build.py", "host.rs", "experiments/scriptc/celld/host.rs",
    )]
    for directory in ("app", "application", "guest", "load"):
        paths.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and not any(part in {
                         "target", ".celld", ".wrangler", "__pycache__",
                     } for part in path.relative_to(ROOT / directory).parts))
    return {str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)}


def run(command, cwd=ROOT):
    print(" ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, check=True)


def clone_source(lock):
    run(["git", "init", "--quiet", CELLD_SOURCE])
    run(["git", "remote", "add", "origin", lock["repository"]], CELLD_SOURCE)
    run(["git", "fetch", "--depth", "1", "origin", lock["revision"]], CELLD_SOURCE)
    run(["git", "checkout", "--detach", "FETCH_HEAD"], CELLD_SOURCE)


def prepare_source():
    lock = json.loads((ROOT / "celld.json").read_text())
    BUILD.mkdir(exist_ok=True)
    if not CELLD_SOURCE.exists():
        clone_source(lock)
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=CELLD_SOURCE, text=True,
    ).strip()
    if revision != lock["revision"]:
        raise RuntimeError(f"Expected celld {lock['revision']}, found {revision}")
    patch = ROOT / "patches/celld.patch"
    applies = subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=CELLD_SOURCE,
        capture_output=True, text=True,
    )
    if applies.returncode == 0:
        run(["git", "apply", patch], CELLD_SOURCE)
    else:
        applied = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch)],
            cwd=CELLD_SOURCE, capture_output=True, text=True,
        )
        if applied.returncode != 0:
            backup = pathlib.Path(tempfile.mkdtemp(prefix="celld-backup-", dir=BUILD)) / "celld"
            CELLD_SOURCE.rename(backup)
            print(f"Preserved generated checkout at {backup}", flush=True)
            clone_source(lock)
            run(["git", "apply", patch], CELLD_SOURCE)
    destination = CELLD_SOURCE / "research/workers-chat"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("app", "application"):
        shutil.copytree(
            ROOT / name, destination / name, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("target", ".celld", ".wrangler", "__pycache__"),
        )
    shutil.copy2(ROOT / "host.rs", destination / "host.rs")
    shutil.copy2(ROOT / "experiments/scriptc/celld/host.rs", destination / "scriptc_host.rs")


def check_build():
    manifest = json.loads((BUILD / "build.json").read_text())
    if manifest["inputs"] != inputs():
        raise RuntimeError("Build inputs changed; run python3 bench/build.py")
    for path, expected in manifest["binaries"].items():
        if digest(ROOT / path) != expected:
            raise RuntimeError(f"Built binary differs: {path}; run python3 bench/build.py")
    return manifest


def main():
    esbuild = ROOT / "node_modules/.bin/esbuild"
    if not esbuild.is_file():
        raise RuntimeError("Install the pinned bundler with vp install --frozen-lockfile")
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    run(["rustup", "toolchain", "install", toolchain, "--no-self-update", "--profile", "minimal",
         "--target", "wasm32-unknown-unknown"])
    prepare_source()
    cargo = ["cargo", f"+{toolchain}", "build", "--locked", "--release"]
    run(cargo + [
        "-p", "celld", "--bin", "celld", "--features", "research-chat",
        "--target-dir", BUILD / "host-target",
        "--config", "profile.release.opt-level=3",
        "--config", 'profile.release.lto="thin"',
        "--config", "profile.release.codegen-units=16",
        "--config", "profile.release.strip=false",
    ], CELLD_SOURCE)
    run(cargo + [
        "--target", "wasm32-unknown-unknown", "--target-dir", BUILD / "guest-target",
    ], ROOT / "guest")
    run(cargo + ["--target-dir", BUILD / "load-target"], ROOT / "load")
    manifest = {
        "celld": json.loads((ROOT / "celld.json").read_text()),
        "rustc": subprocess.check_output(["rustc", f"+{toolchain}", "--version"], text=True).strip(),
        "esbuild": subprocess.check_output([str(esbuild), "--version"], text=True).strip(),
        "inputs": inputs(),
        "binaries": {str(path.relative_to(ROOT)): digest(path) for path in BINARIES},
    }
    (BUILD / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Built celld, the Rust Wasm application, and both load clients.", flush=True)


if __name__ == "__main__":
    main()

import hashlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = ROOT / "experiments/basecamp"
SOURCE = ROOT / ".build/basecamp-source"
OUTPUT = ROOT / ".build/basecamp-host"
BINARY = OUTPUT / "campfire"
LOAD = ROOT / ".build/load-target/release/campfire-original-load"


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def run(command, cwd=ROOT, **kwargs):
    print(" ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, check=True, **kwargs)


def sources():
    paths = list(HERE.glob("*.py")) + [HERE / "load.rs", HERE / "basecamp.json"]
    paths += [ROOT / p for p in ("load/Cargo.toml", "load/Cargo.lock", "bench/fixture.py", "bench/metrics.py")]
    paths += [SOURCE / p for p in ("Cargo.toml", "Cargo.lock", ".gitmodules", "MIT-LICENSE", "crates/db/src/schema.sql", "crates/db/src/schema.rs")]
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths)}


def check_source():
    lock = json.loads((HERE / "basecamp.json").read_text())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    reference = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE / "reference", text=True).strip()
    assert revision == lock["revision"] and reference == lock["reference_revision"]
    for path in (SOURCE, SOURCE / "reference"):
        subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=path, check=True)
    return lock


def check_build():
    manifest = json.loads((OUTPUT / "build.json").read_text())
    assert manifest["basecamp"] == check_source()
    assert manifest["sources"] == sources(), "Run python3 experiments/basecamp/build_basecamp.py after source changes"
    for path, value in manifest["binaries"].items():
        assert digest(ROOT / path) == value, path
    return manifest


def main():
    lock = json.loads((HERE / "basecamp.json").read_text())
    if not SOURCE.exists():
        SOURCE.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet", SOURCE])
        run(["git", "remote", "add", "origin", lock["repository"]], SOURCE)
        run(["git", "fetch", "--depth", "1", "origin", lock["revision"]], SOURCE)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], SOURCE)
    run(["git", "submodule", "update", "--init", "--depth=1"], SOURCE)
    check_source()
    directories = shlex.split(subprocess.check_output(["pkg-config", "--libs-only-L", "vips"], text=True))
    env = dict(os.environ)
    flags = directories.copy()
    if os.uname().sysname == "Darwin":
        for directory in directories:
            flags += ["-C", "link-arg=-Wl,-rpath," + directory[2:]]
    env["RUSTFLAGS"] = shlex.join(flags)
    run(["rustup", "toolchain", "install", lock["rust"], "--profile", "minimal"])
    run(["cargo", "+" + lock["rust"], "build", "--locked", "--release", "--package", "campfire",
         "--target-dir", ROOT / ".build/basecamp-target", "--jobs", "8"], SOURCE, env=env)
    run(["cargo", "+1.96.0", "build", "--locked", "--release", "--bin", "campfire-original-load",
         "--target-dir", ROOT / ".build/load-target"], ROOT / "load")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / ".build/basecamp-target/release/campfire", BINARY)
    vips_prefix = subprocess.check_output(["pkg-config", "--variable=prefix", "vips"], text=True).strip()
    vips_command = pathlib.Path(vips_prefix) / "bin/vips"
    manifest = {
        "basecamp": lock,
        "build_flags": ["--locked", "--release", "--package", "campfire", "--jobs", "8"],
        "rustc": subprocess.check_output(["rustc", "+" + lock["rust"], "--version"], text=True).strip(),
        "vips": subprocess.check_output(["pkg-config", "--modversion", "vips"], text=True).strip(),
        "vips_features": subprocess.check_output([str(vips_command), "--vips-config"], text=True) if vips_command.is_file() else None,
        "glib": subprocess.check_output(["pkg-config", "--modversion", "glib-2.0"], text=True).strip(),
        "sources": sources(),
        "binaries": {str(p.relative_to(ROOT)): digest(p) for p in (BINARY, LOAD)},
    }
    (OUTPUT / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Built the unmodified Basecamp application and its benchmark client.", flush=True)


if __name__ == "__main__":
    main()

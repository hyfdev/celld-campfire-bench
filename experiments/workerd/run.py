import argparse
import contextlib
import http.client
import json
import os
import pathlib
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
import run as shared
from fixture import make_oracle
from metrics import usage, TIMEBASE
from build_workerd import BINARY, GUEST, BINARIES, OUTPUT, check_build, digest

HERE = ROOT / "experiments/workerd"
RUNS = ROOT / ".runs/workerd"
ENGINES = ["js", "wasm-js", "wasm-v8"]
PORT = 19880


def write_configs():
    RUNS.mkdir(parents=True, exist_ok=True)
    for source, target in ((ROOT / "application/worker.js", "worker.js"),
                           (ROOT / "application/wasm-worker.js", "wasm-worker.js"),
                           (HERE / "native-worker.js", "native-worker.js"), (GUEST, "chat.wasm")):
        shutil.copy2(source, RUNS / target)
    for engine, main in (("js", "worker.js"), ("wasm-js", "wasm-worker.js"), ("wasm-v8", "native-worker.js")):
        wasm = ', (name = "chat.wasm", wasm = embed "chat.wasm")' if engine == "wasm-js" else ""
        (RUNS / f"{engine}.capnp").write_text('''using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name = "chat", worker = (
      modules = [(name = "worker.js", esModule = embed "%s")%s],
      compatibilityDate = "2026-09-01",
      bindings = [(name = "ROOMS", durableObjectNamespace = "ChatRoom")],
      durableObjectNamespaces = [(
        className = "ChatRoom", uniqueKey = "campfire-workerd-bench", enableSql = true
      )],
      durableObjectStorage = (localDisk = "storage")
    )),
    (name = "storage", disk = (path = "state", writable = true))
  ],
  sockets = [(name = "http", address = "127.0.0.1:%s", http = (), service = "chat")]
);
''' % (main, wasm, PORT))


@contextlib.contextmanager
def server(engine, label, restore=True):
    assert engine in ENGINES
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", PORT))
    data = RUNS / "state" / engine
    if restore:
        shared.restore_directory(RUNS / "seed", data)
    data.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "WORKERD_CHAT_WASM"}
    if engine == "wasm-v8":
        env["WORKERD_CHAT_WASM"] = str(GUEST)
    log_path = RUNS / f"{label}.log"
    with log_path.open("w+") as log:
        start = time.perf_counter()
        process = subprocess.Popen([str(BINARY), "serve", str(RUNS / f"{engine}.capnp"),
                                    "-d", f"storage={data}"], cwd=RUNS, env=env, stdout=log, stderr=log)
        connection = None
        try:
            for _ in range(20000):
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
                try:
                    status, _, body = shared.request(connection, "GET", "/")
                    if status == 200 and body == (ROOT / "application/index.html").read_bytes():
                        break
                    log.seek(0)
                    raise RuntimeError(f"Unexpected root response: {status} {body[:100]!r}\n{log.read()}")
                except (OSError, http.client.HTTPException):
                    connection.close()
                time.sleep(.001 if label.startswith("cold-") else .01)
            else:
                raise RuntimeError("workerd did not become ready")
            yield {"process": process, "connection": connection, "base": f"http://127.0.0.1:{PORT}",
                   "data": data, "spawned_at": start, "ready_at": time.perf_counter(), "log": log_path}
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.returncode not in (0, -signal.SIGTERM, -signal.SIGKILL):
                log.seek(0)
                raise RuntimeError(f"workerd exited with {process.returncode}\n{log.read()}")


def prepare():
    write_configs()
    state = RUNS / "state/js"
    if state.exists():
        shutil.rmtree(state)
    with server("js", "prepare", restore=False) as s:
        for room in range(1, 51):
            shared.checked_read(s["connection"], room, json_response=True)
    shared.audit(state, {})
    shared.restore_directory(state, RUNS / "seed")
    make_oracle(RUNS / "oracle.json")
    print("Prepared one common seed: 50 workerd Durable Objects, 50,000 messages.", flush=True)


def provenance():
    manifest = check_build()
    paths = {ROOT / path for path in manifest["inputs"]}
    paths |= {ROOT / "bench" / name for name in ("run.py", "fixture.py", "metrics.py", "build.py", "aggregate.py")}
    paths |= {OUTPUT / "build.json", *BINARIES}
    paths |= set(RUNS.glob("*.capnp"))
    hashes = {}
    for path in sorted(paths):
        key = str(path.relative_to(ROOT))
        hashes[key] = digest(path)
        if path not in BINARIES:
            target = RUNS / "sources" / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return {"workerd": manifest["workerd"], "build_flags": manifest["build_flags"],
            "sources": hashes, "source_snapshot": "sources",
            "seed_sha256": {str(p.relative_to(RUNS / "seed")): digest(p)
                            for p in sorted((RUNS / "seed").rglob("*")) if p.is_file()},
            "recorded_only": {str(p.relative_to(ROOT)): "Measured binary digest; rebuild from source." for p in BINARIES}}


def validate(engines):
    shared.validate(engines=engines, server_factory=server, run_directory=RUNS,
                    metadata=provenance(), restart_reset=lambda data: None,
                    restart_source="workerd local SQLite directory after process restart")


def benchmark(seconds, rounds, engines):
    make_oracle(RUNS / "oracle.json", versions=max(32, 1 + int(seconds * 100 / 50 + 1)))
    result = {"complete": False, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "platform": platform.platform(), "cpu": subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip(),
              "memory_bytes": int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)),
              "logical_cpus": int(subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"], text=True)),
              "mach_timebase": {"numer": TIMEBASE.numer, "denom": TIMEBASE.denom},
              "thermal_before": subprocess.check_output(["pmset", "-g", "therm"], text=True),
              "engines": engines, "rounds": rounds, "seconds": seconds, **provenance(), "trials": []}
    for trial in range(rounds):
        order = engines[trial % len(engines):] + engines[:trial % len(engines)]
        for case in shared.CASES:
            for engine in order:
                result["trials"].append(shared.timed(engine, case, 32, trial, seconds,
                                                   server_factory=server, run_directory=RUNS))
                (RUNS / "results.json").write_text(json.dumps(result, indent=2) + "\n")
        for rooms, listeners in ((1, 32), (5, 8)):
            for engine in order:
                result["trials"].append(shared.timed(engine, f"ws-{rooms}x{listeners}", rooms * (listeners + 1),
                                                   trial, seconds, rooms, listeners,
                                                   server_factory=server, run_directory=RUNS))
                (RUNS / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    result["complete"] = True
    result["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["thermal_after"] = subprocess.check_output(["pmset", "-g", "therm"], text=True)
    (RUNS / "results.json").write_text(json.dumps(result, indent=2) + "\n")


def cold(rounds, engines):
    result = {"complete": False, "rounds": rounds, "engines": engines, **provenance(), "trials": []}
    for trial in range(rounds):
        order = engines[trial % len(engines):] + engines[:trial % len(engines)]
        for engine in order:
            with server(engine, f"cold-{trial}-{engine}") as s:
                before = time.perf_counter()
                shared.checked_read(s["connection"], 1)
                end = time.perf_counter()
                memory = usage(s["process"].pid)
                result["trials"].append({"engine": engine, "trial": trial,
                    "process_to_root_ms": (s["ready_at"] - s["spawned_at"]) * 1000,
                    "first_room_ms": (end - before) * 1000,
                    "process_to_first_page_ms": (end - s["spawned_at"]) * 1000,
                    "cpu_ms": (memory["user_ns"] + memory["system_ns"]) / 1e6, "memory": memory})
            (RUNS / "cold-results.json").write_text(json.dumps(result, indent=2) + "\n")
            print(f"Cold {engine} round={trial}: {result['trials'][-1]['process_to_first_page_ms']:.2f} ms", flush=True)
    result["complete"] = True
    (RUNS / "cold-results.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "validate", "bench", "cold", "all"])
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--cold-rounds", type=int, default=10)
    parser.add_argument("--engines", nargs="+", choices=ENGINES, default=ENGINES)
    args = parser.parse_args()
    assert args.seconds > 0 and args.rounds > 0 and args.cold_rounds > 0
    check_build()
    write_configs()
    if args.action in ("prepare", "all"):
        prepare()
    if args.action in ("validate", "all"):
        validate(args.engines)
    if args.action in ("bench", "all"):
        benchmark(args.seconds, args.rounds, args.engines)
    if args.action in ("cold", "all"):
        cold(args.cold_rounds, args.engines)


if __name__ == "__main__":
    main()

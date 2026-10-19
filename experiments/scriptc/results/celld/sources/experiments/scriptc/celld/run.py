import argparse
import hashlib
import json
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
EXPERIMENT = HERE.parent
sys.path.insert(0, str(ROOT / "bench"))
import run as bench


def configure():
    bench.RUNS = EXPERIMENT / ".build/celld-runs"
    bench.PROJECT = bench.RUNS / "app"
    bench.STATE = bench.PROJECT / ".celld/dev"
    bench.ENGINES = ["js", "scriptc-wasm", "native"]
    original_env = bench.env

    def env(engine):
        values = original_env(engine)
        values["WORKERS_CHAT_TS_WASM"] = str(EXPERIMENT / ".build/chat.wasm")
        return values

    bench.env = env
    original_provenance = bench.provenance

    def provenance():
        data = original_provenance()
        for name in ("app.ts", "data.ts", "host.rs", "wasm.json", "build.py", "run.py"):
            source = HERE / name
            relative = source.relative_to(ROOT)
            data["sources"][str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
            target = bench.RUNS / "sources" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for name in ("chat.wasm",):
            source = EXPERIMENT / ".build" / name
            relative = str(source.relative_to(ROOT))
            data["sources"][relative] = hashlib.sha256(source.read_bytes()).hexdigest()
            data["recorded_only"][relative] = "Measured scriptc application binary."
        manifest = EXPERIMENT / ".build/celld-build.json"
        data["scriptc_build"] = json.loads(manifest.read_text())
        return data

    bench.provenance = provenance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["validate", "bench", "all"], default="all")
    args = parser.parse_args()
    configure()
    bench.check_build()
    manifest = json.loads((EXPERIMENT / ".build/celld-build.json").read_text())
    for relative, expected in {**manifest["inputs"], **manifest["binaries"]}.items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Changed scriptc build input: {relative}; run python3 experiments/scriptc/celld/build.py")
    if not (bench.RUNS / "seed-celld").exists():
        bench.prepare()
    if args.phase in ("validate", "all"):
        bench.validate()
        validation_path = bench.RUNS / "validation.json"
        validation = json.loads(validation_path.read_text())
        validation["complete"] = False
        validation_path.write_text(json.dumps(validation, indent=2) + "\n")
        validation["maximum_message_rows"] = []
        for engine in bench.ENGINES:
            posts = []
            with bench.server(engine, f"maximum-message-rows-{engine}") as server:
                connection = server["connection"]
                for room in range(1, 51):
                    bench.checked_read(connection, room, json_response=True)
                for index in range(50):
                    text = "\0" * 8192
                    bench.checked_post(connection, 1, 1, text, index + 2)
                    posts.append((1, text))
                for json_response in (False, True):
                    bench.checked_read(connection, 1, 1, json_response, posts, cache=True, hit=False)
                    bench.checked_read(connection, 1, 1, json_response, posts, cache=True, hit=not json_response)
            audit = bench.audit(server["data"], {1: posts})
            validation["maximum_message_rows"].append({"engine": engine, "posts": 50,
                "body_bytes": 8192, "body": "NUL bytes", "response_checks": 104, "database_audit": audit})
            print(f"Validated {engine}: 50 maximum-size messages, HTML/JSON, and page caching", flush=True)
        validation["complete"] = True
        validation_path.write_text(json.dumps(validation, indent=2) + "\n")
    if args.phase in ("bench", "all"):
        bench.benchmark(3, 3, [32])
    destination = EXPERIMENT / "results/celld"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("validation.json", "results.json"):
        source = bench.RUNS / name
        if source.exists():
            shutil.copy2(source, destination / name)
    shutil.copytree(bench.RUNS / "sources", destination / "sources", dirs_exist_ok=True)


if __name__ == "__main__":
    main()

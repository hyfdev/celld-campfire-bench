import hashlib
import json
import pathlib
import statistics


def verify_sources(root, data):
    assert set(data["recorded_only"]) <= set(data["sources"])
    snapshot = root / data["source_snapshot"]
    for path, digest in data["sources"].items():
        if path not in data["recorded_only"]:
            assert hashlib.sha256((snapshot / path).read_bytes()).hexdigest() == digest, path


def load(directory):
    root = pathlib.Path(directory)
    results = json.loads((root / "results.json").read_text())
    cold = json.loads((root / "cold-results.json").read_text())
    validation = json.loads((root / "validation.json").read_text())
    datasets = [results, cold, validation]
    assert all(data["complete"] for data in datasets), "Measurements are incomplete"
    assert results["sources"] == cold["sources"] == validation["sources"]
    assert results["source_snapshot"] == cold["source_snapshot"] == validation["source_snapshot"]
    assert results["recorded_only"] == cold["recorded_only"] == validation["recorded_only"]
    verify_sources(root, results)
    for row in results["trials"]:
        assert not row["errors"], row
        assert row["database_audit"]["integrity"] == "ok", row
        assert row["database_audit"]["rooms"] == 50, row
    assert {row["engine"] for row in results["trials"]} == {row["engine"] for row in cold["trials"]} == {row["engine"] for row in validation["engines"]}
    engines = ["js"]
    if any(r["engine"] == "wasm-js" for r in results["trials"]):
        engines.append("wasm-js")
    engines += ["wasm-v8", "wasm"]
    if any(r["engine"] == "scriptc-wasm" for r in results["trials"]):
        engines.append("scriptc-wasm")
    engines.append("native")
    metrics = ["cpu_us_per_operation", "cpu_percent_one_core", "client_cpu_percent_one_core", "p50_ms", "p99_ms"]
    aggregate = {"http": [], "websocket": [], "cold": [], "checks": {}}

    for case in ["uncached-html", "uncached-json", "cached-html", "post", "mixed", "ws-1x32", "ws-5x8"]:
        concurrencies = sorted({r["concurrency"] for r in results["trials"] if r["case"] == case})
        ws = case.startswith("ws-")
        for concurrency in concurrencies:
            for engine in engines:
                rows = [r for r in results["trials"] if (r["case"], r["concurrency"], r["engine"]) == (case, concurrency, engine)]
                assert len(rows) == len({r["trial"] for r in results["trials"]}) and all(not r["errors"] for r in rows)
                row = {"case": case, "concurrency": concurrency, "engine": engine}
                rate = "messages_per_s" if ws else "requests_per_s"
                for key in metrics + [rate] + (["deliveries_per_s"] if ws else ["writes_per_s", "reads_per_s"]):
                    row[key] = statistics.median(r[key] for r in rows)
                row["throughput_min"] = min(r[rate] for r in rows)
                row["throughput_max"] = max(r[rate] for r in rows)
                row["memory_mib"] = statistics.median(r["max_footprint_bytes"] / 2**20 for r in rows)
                row["rss_mib"] = statistics.median(r["max_rss_bytes"] / 2**20 for r in rows)
                if ws:
                    assert all(r["read_acknowledgments_enabled"] for r in rows)
                    row["sender_p99_ms"] = statistics.median(r["sender_p99_ms"] for r in rows)
                    row["read_acknowledgments_per_s"] = statistics.median(r["read_acknowledgments"] / r["elapsed_s"] for r in rows)
                if case == "mixed":
                    row["cache_hit_percent"] = statistics.median(r["cache_hits"] / r["reads"] * 100 for r in rows)
                aggregate["websocket" if ws else "http"].append(row)

    for engine in engines:
        rows = [r for r in cold["trials"] if r["engine"] == engine]
        assert len(rows) == len({r["trial"] for r in cold["trials"]})
        row = {"engine": engine}
        for key in ["process_to_root_ms", "first_room_ms", "process_to_first_page_ms"]:
            row[key] = statistics.median(r[key] for r in rows)
            row[key + "_min"] = min(r[key] for r in rows)
            row[key + "_max"] = max(r[key] for r in rows)
        row["footprint_mib"] = statistics.median(r["memory"]["footprint_bytes"] / 2**20 for r in rows)
        aggregate["cold"].append(row)

    aggregate["checks"] = {
        "http_trials": sum(not r["case"].startswith("ws-") for r in results["trials"]),
        "websocket_trials": sum(r["case"].startswith("ws-") for r in results["trials"]),
        "http_requests": sum(r.get("successful", 0) for r in results["trials"]),
        "http_posts": sum(r.get("writes", 0) for r in results["trials"]),
        "websocket_messages": sum(r.get("messages", 0) for r in results["trials"]),
        "websocket_deliveries_including_sender": sum(r.get("total_frame_deliveries", 0) for r in results["trials"]),
        "websocket_read_acknowledgments": sum(r.get("read_acknowledgments", 0) for r in results["trials"]),
        "dedicated_http_checks": sum(r["response_checks"] for r in validation["engines"]),
        "database_audits": len(results["trials"]),
        "max_client_cpu_percent_one_core": max(r["client_cpu_percent_one_core"] for r in results["trials"]),
        "total_cold_trials": len(cold["trials"]),
    }
    return results, cold, validation, aggregate


def load_compile_cache(directory):
    root = pathlib.Path(directory)
    path = root / "cold-cache-results.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    assert data["complete"], "Compilation-cache measurements are incomplete"
    verify_sources(root, data)
    population = data["cache_population"]
    assert population["root_validated"] and population["first_page_validated"]
    assert population["wasmtime"]["cache_enabled"]
    assert (population["wasmtime"]["cache_hits"], population["wasmtime"]["cache_misses"]) == (0, 1)
    engines = ["js"]
    engines += [engine for engine in ("wasm-js", "wasm-v8")
                if any(r["engine"] == engine for r in data["trials"])]
    engines += ["wasm", "wasm-cached"]
    if any(r["engine"] == "scriptc-wasm" for r in data["trials"]):
        engines.append("scriptc-wasm")
    engines.append("native")
    expected = {(engine, trial) for engine in engines for trial in range(data["rounds"])}
    assert len(data["trials"]) == len(expected)
    assert {(r["engine"], r["trial"]) for r in data["trials"]} == expected
    for r in data["trials"]:
        assert r["root_validated"] and r["first_page_validated"]
        assert r["cpu_ms"] == (r["memory"]["user_ns"] + r["memory"]["system_ns"]) / 1e6
        if r["engine"] in ("wasm", "wasm-cached"):
            module = r["wasmtime"]
            cached = r["engine"] == "wasm-cached"
            assert module["cache_enabled"] == cached and module["module_ms"] > 0
            assert (module["cache_hits"], module["cache_misses"]) == ((1, 0) if cached else (0, 0))
        else:
            assert r["wasmtime"] is None
    rows = []
    for engine in engines:
        trials = [r for r in data["trials"] if r["engine"] == engine]
        row = {"engine": engine}
        for key in ("process_to_first_page_ms", "cpu_ms"):
            row[key] = statistics.median(r[key] for r in trials)
        row["footprint_mib"] = statistics.median(r["memory"]["footprint_bytes"] / 2**20 for r in trials)
        if engine in ("wasm", "wasm-cached"):
            row["module_ms"] = statistics.median(r["wasmtime"]["module_ms"] for r in trials)
        rows.append(row)
    return data, rows

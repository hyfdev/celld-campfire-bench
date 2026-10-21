import argparse
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
from aggregate import load

ENGINES = ["js", "wasm-js", "wasm-v8"]
LABELS = {
    "js": "JavaScript / V8 (baseline)",
    "wasm-js": "Rust Wasm / V8 + JS bindings",
    "wasm-v8": "Rust Wasm / V8 + direct C++ host calls (prototype)",
}
CASES = {
    "uncached-html": "Server-rendered HTML (without in-memory page caching)",
    "uncached-json": "Read messages as JSON (no response cache)",
    "cached-html": "Server-rendered HTML (with in-memory page caching)",
    "post": "Post a message; reply after saving it",
    "mixed": "Mixed reads and posts (100 posts/s target)",
    "ws-1x32": "1 room, 32 receiving connections",
    "ws-5x8": "5 rooms, 8 receiving connections per room",
}


def number(value, baseline, places=2):
    return f"{value:,.{places}f} ({value / baseline:.2f}×)"


def table(rows, *, websocket=False):
    rate = "messages_per_s" if websocket else "requests_per_s"
    unit = "messages/s" if websocket else "requests/s"
    baseline = next(r for r in rows if r["engine"] == "js")
    lines = [
        f"| Application runtime | Total {unit} | CPU use (%; 100% = 1 core) | CPU time/operation (µs) | Peak process memory (MiB) | p99 response time (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    metrics = [(rate, 0), ("cpu_percent_one_core", 1), ("cpu_us_per_operation", 1),
               ("memory_mib", 1), ("p99_ms", 2)]
    for row in rows:
        values = " | ".join(number(row[key], baseline[key], places) for key, places in metrics)
        lines.append(f"| {LABELS[row['engine']]} | {values} |")
    return "\n".join(lines)


def verified(directory):
    raw, cold, validation, summary = load(directory, engines=ENGINES)
    assert raw["engines"] == cold["engines"] == ENGINES
    cases = list(CASES)
    expected = {(engine, case, trial) for engine in ENGINES for case in cases for trial in range(raw["rounds"])}
    assert len(raw["trials"]) == len(expected)
    assert {(r["engine"], r["case"], r["trial"]) for r in raw["trials"]} == expected
    expected_cold = {(engine, trial) for engine in ENGINES for trial in range(cold["rounds"])}
    assert len(cold["trials"]) == len(expected_cold)
    assert {(r["engine"], r["trial"]) for r in cold["trials"]} == expected_cold
    assert raw["seed_sha256"] == cold["seed_sha256"] == validation["seed_sha256"]
    assert len(raw["seed_sha256"]) >= 50
    assert all(r["before_restart"]["integrity"] == r["after_restart"]["integrity"] == "ok"
               for r in validation["engines"])
    assert all(r["cpu_ms"] == (r["memory"]["user_ns"] + r["memory"]["system_ns"]) / 1e6
               for r in cold["trials"])
    return raw, cold, validation, summary


def readme(raw, summary):
    rows = [r for r in summary["http"] if r["case"] == "uncached-html"]
    return f"""### workerd: server-rendered HTML (without in-memory page caching), 32 simultaneous clients

The same chat workload on workerd {raw['workerd']['version']}, with one shared release build and SQLite seed. Apple M4 Pro, 48 GiB RAM · {raw['started_at'][:10]}. Medians of {raw['rounds']} {raw['seconds']:g}-second trials; ratios use this workerd JS baseline.

{table(rows)}

The direct path is a patched workerd prototype: HTTP → V8 Wasm → C++ host functions → workerd DO storage and WebSockets. It executes no JS application or binding callback. [Complete report and raw data](results/2026-10-21-workerd/REPORT.md) · [Reproduce](experiments/workerd/README.md)."""


def render(raw, cold, validation, summary):
    lines = [
        "# workerd chat benchmark", "",
        f"Apple M4 Pro, 48 GiB RAM, {raw['platform']} · {raw['started_at'][:10]}. workerd {raw['workerd']['tag']} at `{raw['workerd']['revision']}`. All three modes use the same patched release executable and the same seed: 50 Durable Objects with 1,000 messages each.", "",
        "## Request and binding paths", "",
        "| Mode | Request handler | SQL and WebSocket access |",
        "| --- | --- | --- |",
        "| `js` | Standard workerd JS Worker and DO handlers | Standard JS bindings |",
        "| `wasm-js` | Standard JS Worker and DO entry → Rust Wasm in V8 | Wasm imports implemented in JS → standard workerd bindings |",
        "| `wasm-v8` | Patched workerd native entry → the same Rust Wasm in V8 | C++ Wasm import callbacks → real workerd SQLite and WebSocket implementations |", "",
        "The direct prototype keeps workerd's actor routing, activation, input gate, SQLite backend and automatic transactions, output gate, and WebSocket manager. Its startup module declares exports; its JS constructor, fetch handlers, and WebSocket handlers throw if invoked. V8's native function ABI and Wasm-to-host wrappers remain. No JS application or binding callback executes on the direct path.", "",
        "This measures complete request paths. The direct path also removes JS Request/Response objects, SQL cursor and row conversion, and JS WebSocket wrappers. The difference is not an isolated timing of a single Wasm-to-JS transition. The prototype covers this application's API and synchronous Wasm handlers; full Workers API compatibility, tracing, cancellation, and tenant CPU limits are outside its scope.", "",
        f"## HTTP: 32 simultaneous clients", "",
        f"Medians of {raw['rounds']} {raw['seconds']:g}-second trials, with 2,000 warmup reads per trial. Each request authenticates a user and checks membership. Uncached reads query SQLite and return the latest 50 messages; cached HTML uses an application page cache. Database and operating-system caches remain active. Each client waits for a response before its next request.", "",
    ]
    for case, label in list(CASES.items())[:5]:
        rows = [r for r in summary["http"] if r["case"] == case]
        lines += [f"### {label}", "", table(rows), ""]
    lines += ["## WebSocket chat", "",
              "Each room has one sender plus the receiving connections shown. The sender waits for storage, broadcast, and every participant's HTTP read confirmation before posting again. CPU time is per posted message; p99 is the completed broadcast-and-read-confirmation time.", ""]
    for case, label in list(CASES.items())[5:]:
        rows = [r for r in summary["websocket"] if r["case"] == case]
        lines += [f"### {label}", "", table(rows, websocket=True), ""]
    lines += ["## Fresh-process startup", "",
              f"Medians of {cold['rounds']} fresh processes per mode. Start → first room response includes process startup, the validated root page, and the first validated room page. Filesystem caches stay warm; no application compiled-module cache is configured.", "",
              "| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |",
              "| --- | ---: | ---: | ---: |"]
    medians = {}
    for engine in ENGINES:
        rows = [r for r in cold["trials"] if r["engine"] == engine]
        medians[engine] = [statistics.median(r["process_to_first_page_ms"] for r in rows),
                           statistics.median(r["cpu_ms"] for r in rows),
                           statistics.median(r["memory"]["footprint_bytes"] / 2**20 for r in rows)]
    for engine in ENGINES:
        values = " | ".join(number(v, b, 1 if i == 2 else 2)
                            for i, (v, b) in enumerate(zip(medians[engine], medians["js"])))
        lines.append(f"| {LABELS[engine]} | {values} |")
    checks = summary["checks"]
    lines += ["", "## Validation and measurement", "",
              f"All {checks['http_trials']} HTTP pressure trials and {checks['websocket_trials']} WebSocket pressure trials completed with zero client errors. Every trial ends with an audit of all 50 room databases. The dedicated functional suite checks {checks['dedicated_http_checks']:,} HTTP responses across the three modes, ordered broadcasts, cache invalidation, permissions, Unicode/NUL messages, and process-restart persistence. All {checks['total_cold_trials']} startup trials validate response bytes.", "",
              "CPU and memory cover the whole workerd server process. CPU includes all its threads; memory is the macOS process footprint sampled every 100 ms during load. Ratios use the unrounded median for JS in the same workload. The load client runs on the same machine and its CPU is recorded separately. The three modes run sequentially with their order rotated between rounds.", "",
              "These are single-node local workerd measurements. They are separate from the celld cohort: workerd's local SQLite persistence and celld's LTX persistence have different implementations. They do not measure Cloudflare's hosted network, distributed storage, or service limits.", "",
              "[Load trials](results.json) · [Startup trials](cold-results.json) · [Functional validation](validation.json) · [Measured source snapshots](sources/) · [Reproduction instructions](../../experiments/workerd/README.md)", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=pathlib.Path, default=ROOT / "results/2026-10-21-workerd")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--readme", action="store_true")
    args = parser.parse_args()
    raw, cold, validation, summary = verified(args.directory)
    report = render(raw, cold, validation, summary)
    if args.check:
        assert (args.directory / "REPORT.md").read_text() == report, "REPORT.md differs from measurements"
        print(f"Verified {len(raw['trials'])} load trials, {len(cold['trials'])} startup trials, functional checks, and source hashes.")
    else:
        print(readme(raw, summary) if args.readme else report, end="\n" if args.readme else "")


if __name__ == "__main__":
    main()

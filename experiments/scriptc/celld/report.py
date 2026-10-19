import argparse
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "bench"))
from aggregate import verify_sources
from render import relative

RESULTS = ROOT / "experiments/scriptc/results/celld"
LABELS = {
    "js": "JavaScript / V8",
    "scriptc-wasm": "TypeScript → scriptc Wasm / Wasmtime",
    "native": "Native Rust",
}
CASES = {
    "uncached-html": "Server-rendered HTML (without in-memory page caching)",
    "uncached-json": "Read messages as JSON (no response cache)",
    "cached-html": "Server-rendered HTML (with in-memory page caching)",
    "post": "Post a message; reply after saving it",
    "mixed": "Read message history while posting new messages",
    "ws-1x32": "WebSocket chat: 1 room, 32 receiving connections",
    "ws-5x8": "WebSocket chat: 5 rooms, 8 receiving connections per room",
}


def report():
    raw = json.loads((RESULTS / "results.json").read_text())
    validation = json.loads((RESULTS / "validation.json").read_text())
    assert raw["complete"] and validation["complete"]
    assert raw["sources"] == validation["sources"]
    assert raw["scriptc_build"] == validation["scriptc_build"]
    verify_sources(RESULTS, raw)
    expected = {(case, engine, trial) for case in CASES for engine in LABELS for trial in range(3)}
    assert len(raw["trials"]) == len(expected)
    assert {(r["case"], r["engine"], r["trial"]) for r in raw["trials"]} == expected
    for row in raw["trials"]:
        assert not row["errors"]
        assert row["database_audit"]["integrity"] == "ok" and row["database_audit"]["rooms"] == 50
    assert {r["engine"] for r in validation["engines"]} == set(LABELS)
    assert all(r["response_checks"] == 716 for r in validation["engines"])
    large_rows = validation["maximum_message_rows"]
    assert {r["engine"] for r in large_rows} == set(LABELS)
    assert all(r["posts"] == 50 and r["body_bytes"] == 8192 and r["response_checks"] == 104
               and r["database_audit"]["integrity"] == "ok" for r in large_rows)
    lines = ["# scriptc on celld: full chat application", "",
             f"{raw['cpu']}, {raw['memory_bytes']/2**30:g} GiB RAM, macOS arm64 · {raw['started_at'][:10]}. Medians of three 3-second trials; one celld Tokio thread. All ratios use JavaScript as the baseline.", ""]
    for case, title in CASES.items():
        ws = case.startswith("ws-")
        rate = "messages_per_s" if ws else "requests_per_s"
        operation = "message" if ws else "request"
        lines += [f"## {title}", ""]
        if ws:
            lines += ["One sender per room waits for persistence, broadcast, and every participant's HTTP read confirmation before sending the next message.", ""]
        elif case == "mixed":
            lines += ["32 reading clients and one client attempting 100 posts/s; throughput includes reads and posts.", ""]
        else:
            lines += ["32 simultaneous clients.", ""]
        lines += [f"| Application runtime | {'Messages' if ws else 'Requests'}/s; min–max across trials | CPU use (%; 100% = 1 core) | CPU µs/{operation} | Peak process memory (MiB) | p99 (ms) |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        medians = {}
        for engine in LABELS:
            rows = [r for r in raw["trials"] if r["case"] == case and r["engine"] == engine]
            medians[engine] = [statistics.median(r[key] / scale for r in rows)
                               for key, scale in ((rate, 1), ("cpu_percent_one_core", 1),
                                                  ("cpu_us_per_operation", 1), ("max_footprint_bytes", 2**20), ("p99_ms", 1))]
        for engine, values in medians.items():
            cells = [relative(value, baseline, spec) for value, baseline, spec
                     in zip(values, medians["js"], (",.0f", ".1f", ".1f", ".1f", ".2f"))]
            rates = [r[rate] for r in raw["trials"] if r["case"] == case and r["engine"] == engine]
            cells[0] += f"<br>{min(rates):,.0f}–{max(rates):,.0f}"
            lines.append("| " + LABELS[engine] + " | " + " | ".join(cells) + " |")
        lines.append("")
        if case == "mixed":
            lines += ["| Application runtime | Reads/s | Posts/s | Reads served from page cache (%) |",
                      "| --- | ---: | ---: | ---: |"]
            for engine in LABELS:
                rows = [r for r in raw["trials"] if r["case"] == case and r["engine"] == engine]
                values = [statistics.median(r["reads_per_s"] for r in rows),
                          statistics.median(r["writes_per_s"] for r in rows),
                          statistics.median(r["cache_hits"] / r["reads"] * 100 for r in rows)]
                lines.append(f"| {LABELS[engine]} | {values[0]:,.0f} | {values[1]:.1f} | {values[2]:.1f} |")
            lines += ["", "The posting client can fall below its 100 posts/s target. The achieved write rates above describe the load each mode completed.", ""]
    lines += ["CPU and physical memory cover the entire celld process. Each trial includes 2,000 warmup reads before timing and a subsequent audit of all 50 room databases. Module compilation and startup occur before load timing.", "",
              f"All {len(raw['trials'])} load trials and {sum(r['response_checks'] for r in validation['engines'] + large_rows):,} dedicated HTTP response checks pass. Validation also checks ordered WebSocket broadcasts, unread counts, and recovery from the object store after deleting the runtime directory. Each mode returns HTML and JSON after receiving 50 messages of 8,192 NUL bytes each, including page-cache behavior when JSON escaping exceeds the 1 MiB page-cache limit.", "",
              "[Load measurements](results.json) · [Validation](validation.json) · [Measured source snapshots](sources/)", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = report()
    if args.check:
        assert (RESULTS / "REPORT.md").read_text() == text, "Report differs from measurements"
        print("Verified measured sources, load trials, validation, and report tables.")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()

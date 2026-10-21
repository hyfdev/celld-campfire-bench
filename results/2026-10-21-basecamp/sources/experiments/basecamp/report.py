import argparse
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
from aggregate import load, verify_sources

CASES = {
    "messages-html": "Messages HTML; response and fragment caches disabled",
    "messages-cached-html": "Messages HTML; original response and fragment caches enabled",
    "room-html": "Full room page; response and fragment caches disabled",
    "post": "Post a message; original caches enabled",
}


def verified(directory, workerd_directory):
    raw = json.loads((directory / "results.json").read_text())
    cold = json.loads((directory / "cold-results.json").read_text())
    validation = json.loads((directory / "validation.json").read_text())
    for data in (raw, cold, validation):
        assert data["complete"]
        verify_sources(directory, data)
        assert data["sources"] == raw["sources"]
        assert data["seed_sha256"] == raw["seed_sha256"]
    expected = {(case, trial) for case in CASES for trial in range(raw["rounds"])}
    assert len(raw["trials"]) == len(expected)
    assert {(r["case"], r["trial"]) for r in raw["trials"]} == expected
    assert [r["trial"] for r in cold["trials"]] == list(range(cold["rounds"]))
    for trial in raw["trials"]:
        assert not trial["errors"] and trial["successful"] > 0
        audit = trial["database_audit"]
        assert audit["integrity"] == "ok" and audit["rooms"] == 50
        posts = trial["successful"] + trial["warmup_writes"] if trial["case"] == "post" else 0
        assert audit["posts"] == audit["acknowledged_posts_checked"] == posts
        assert audit["search_rows"] == 50000 + posts
        assert trial["cpu_us_per_operation"] == trial["cpu_ns"] / trial["successful"] / 1000
    assert all(r["before_restart"]["integrity"] == r["after_restart"]["integrity"] == "ok" for r in validation["cases"])
    other, _, _, summary = load(workerd_directory, engines=["js", "wasm-js", "wasm-v8"])
    assert raw["cohort"] == other["cohort"], "Basecamp and workerd must come from the same comparison run"
    rows = []
    for case in CASES:
        trials = [r for r in raw["trials"] if r["case"] == case]
        row = {"case": case}
        for key in ("requests_per_s", "cpu_percent_one_core", "cpu_us_per_operation", "p99_ms", "average_response_bytes"):
            row[key] = statistics.median(r[key] for r in trials)
        row["memory_mib"] = statistics.median(r["max_footprint_bytes"] / 2**20 for r in trials)
        rows.append(row)
    return raw, cold, validation, rows, other, summary


def number(value, baseline, places):
    return f"{value:,.{places}f} ({value / baseline:.2f}×)"


def result_table(rows, summary, *, full_room=False):
    mapping = {"messages-html": "uncached-html", "messages-cached-html": "cached-html", "post": "post"}
    lines = [
        "| Original application operation | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/request (µs) | Peak process memory (MiB) | p99 response time (ms) | Response size (KiB) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        if row["case"] not in mapping and not full_room:
            continue
        baseline = next((r for r in summary["http"] if r["engine"] == "js" and r["case"] == mapping.get(row["case"])), None)
        metrics = [("requests_per_s", 0), ("cpu_percent_one_core", 1), ("cpu_us_per_operation", 1), ("memory_mib", 1), ("p99_ms", 2)]
        values = " | ".join(number(row[k], baseline[k], p) if baseline else f"{row[k]:,.{p}f}" for k, p in metrics)
        lines.append(f"| {CASES[row['case']]} | {values} | {row['average_response_bytes'] / 1024:,.1f} |")
    return "\n".join(lines)


def readme(raw, rows, summary):
    return "\n".join([
        "### Original Basecamp Rust application", "",
        "The unmodified Basecamp application runs with 32 clients in the same session. Ratios use workerd JS for the corresponding request. The original renders 40 rich-text messages per page; the Workers chat renders 50 plain-text messages. SQL, caching, and write durability also differ, so these are whole-application references.", "",
        result_table(rows, summary), "",
        "[Original application report and raw data](results/2026-10-21-basecamp/REPORT.md) · [Reproduce](experiments/basecamp/README.md).",
    ])


def render(raw, cold, validation, rows, other, summary):
    lines = [
        "# Original Basecamp Rust application", "",
        f"Unmodified [once-campfire-rust](https://github.com/basecamp/once-campfire-rust/tree/{raw['basecamp']['revision']}) at `{raw['basecamp']['revision']}`. Apple M4 Pro, 48 GiB RAM, {raw['platform']} · {raw['started_at'][:10]}. Native Rust {raw['basecamp']['rust']}, release profile with the upstream fat LTO settings.", "",
        "The process serves its original HTTP front end, Axum controllers, Askama views, authentication, rich text, and SQLite database. The fixture contains 50 rooms, 1,000 messages per room, and the same message text and user names as the Workers chat. All timed requests use 32 clients, the same room/user rotation, HTTP/1.1, and identity encoding.", "",
        "## Results", "",
        f"Medians of {raw['rounds']} {raw['seconds']:g}-second trials. Each trial restores the seed and performs 2,000 warmup reads; posts also warm up with 1,000 saved messages.", "",
        "Ratios use the corresponding uncached read, cached read, or post from workerd JS in the same session. The full room page has no matching Workers workload.", "",
        result_table(rows, summary, full_room=True), "",
        "The three workerd modes execute the same Workers-style application and can be compared with one another. The original Basecamp application does different work: 40 richly marked-up messages per page, one SQLite database, cookie authentication, different SQL, and its own cache policies. The Workers chat returns 50 plain-text messages and stores one SQLite database per room. Original writes use SQLite WAL with synchronous=NORMAL; local workerd uses its own SQLite/VFS and automatic transactions. These ratios compare whole applications and do not isolate the overhead of the Workers/DO design.",
    ]
    lines += ["", "## Startup", "",
              f"Medians of {cold['rounds']} fresh processes with the original caches enabled. The wall time runs from spawning the executable through a validated /up health response to receipt of the first authenticated 40-message HTML response; response-content validation runs after timing. Filesystem caches stay warm.", "",
              "| Start → first messages response (ms) | CPU time to first response (ms) | Process memory after first response (MiB) |",
              "| ---: | ---: | ---: |",
              f"| {statistics.median(r['process_to_first_page_ms'] for r in cold['trials']):.2f} | {statistics.median(r['cpu_ms'] for r in cold['trials']):.2f} | {statistics.median(r['memory']['footprint_bytes']/2**20 for r in cold['trials']):.1f} |", "",
              "## Validation and runtime settings", "",
              f"The functional suite validates {sum(r['response_checks'] for r in validation['cases']):,} responses across disabled/default-cache configurations, authenticated pages, rejection of unauthenticated reads, post invalidation, and persistence after restart. Every measured read matches the full bytes of a preflight page whose message IDs and text are independently checked against the fixture. Every acknowledged POST is checked in the message, rich-text, and full-text-search tables after shutdown. All load trials have zero errors and pass SQLite integrity and foreign-key checks.", "",
              "The original executable runs with TOKIO_WORKER_THREADS=1, RAILS_MAX_THREADS=1, and JOB_CONCURRENCY=1. Its database, checkpoint, jobs, and network helper threads remain part of the process. CPU covers every thread; memory is the macOS process footprint sampled every 100 ms. Uncached cases set CAMPFIRE_RESPONSE_CACHE_MB=0 and CAMPFIRE_FRAGMENT_CACHE_MB=0. Cached and POST cases use the original 64 MiB response and 32 MiB fragment limits.", "",
              f"The executable links libvips {raw['vips']} and GLib {raw['glib']}; its feature configuration is recorded in the [build manifest](sources/.build/basecamp-host/build.json). This text workload does not call media handlers or initialize libvips.", "",
              f"The workerd comparison has the same cohort identifier `{raw['cohort']}`. For matching read/post cases the four executables run sequentially with their order rotated by round. Workerd-only JSON, mixed, and WebSocket cases and the original full-room page run in the same session. No published DHH hardware results are mixed into these numbers.", "",
              "[Original load data](results.json) · [Startup data](cold-results.json) · [Functional validation](validation.json) · [Source snapshots](sources/) · [workerd report](../2026-10-21-workerd/REPORT.md) · [Reproduce](../../experiments/basecamp/README.md)", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=pathlib.Path, default=ROOT / "results/2026-10-21-basecamp")
    parser.add_argument("--workerd", type=pathlib.Path, default=ROOT / "results/2026-10-21-workerd")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--readme", action="store_true")
    args = parser.parse_args()
    values = verified(args.directory, args.workerd)
    report = render(*values)
    if args.check:
        assert (args.directory / "REPORT.md").read_text() == report
        print(f"Verified {len(values[0]['trials'])} original-application load trials, {len(values[1]['trials'])} startup trials, functional results, and cohort/source hashes.")
    else:
        print(readme(values[0], values[3], values[5]) if args.readme else report, end="\n" if args.readme else "")


if __name__ == "__main__":
    main()

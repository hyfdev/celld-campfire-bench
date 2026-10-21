import argparse
import contextlib
import datetime
import hashlib
import html
import http.client
import http.cookies
import importlib.util
import json
import os
import pathlib
import platform
import re
import resource
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = ROOT / "experiments/basecamp"
sys.path.insert(0, str(ROOT / "bench"))
import run as shared
from fixture import POST_TEXT, seed_body, user_name, initial_unread
from metrics import usage, TIMEBASE
sys.path.insert(0, str(HERE))
from build_basecamp import BINARY, LOAD, OUTPUT, SOURCE, check_build, digest
sys.path.insert(0, str(ROOT / "experiments/workerd"))
spec = importlib.util.spec_from_file_location("workerd_run", ROOT / "experiments/workerd/run.py")
workerd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workerd)

RUNS = ROOT / ".runs/basecamp"
PORT = 19881
TARGET_PORT = 19882
CASES = ["messages-html", "messages-cached-html", "room-html", "post"]
FROZEN_TIME = "2026-10-21T08:00:00Z"
POST_HTML = "<p>" + html.escape(POST_TEXT, quote=False).replace("\n", "<br>") + "</p>"


def configuration(cached):
    return {"DISABLE_SSL": "1", "RAILS_ENV": "production", "APP_VERSION": "benchmark",
            "CAMPFIRE_FROZEN_TIME": FROZEN_TIME, "TOKIO_WORKER_THREADS": "1",
            "RAILS_MAX_THREADS": "1", "JOB_CONCURRENCY": "1", "CAMPFIRE_LOG": "error",
            "CAMPFIRE_RESPONSE_CACHE_MB": "64" if cached else "0",
            "CAMPFIRE_FRAGMENT_CACHE_MB": "32" if cached else "0",
            "THRUSTER_LOG_REQUESTS": "false", "THRUSTER_GZIP_COMPRESSION_ENABLED": "false"}


def request(connection, method, path, body=b"", cookie="", content_type="application/x-www-form-urlencoded"):
    connection.request(method, path, body=body, headers={"cookie": cookie, "content-type": content_type,
                       "accept-encoding": "identity", "sec-fetch-site": "same-origin",
                       "accept": "text/vnd.turbo-stream.html" if method == "POST" and "/messages" in path else "text/html"})
    response = connection.getresponse()
    data = response.read()
    return response.status, response.getheaders(), data


@contextlib.contextmanager
def server(label, *, cached=True, restore=True):
    RUNS.mkdir(parents=True, exist_ok=True)
    for port in (PORT, TARGET_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    data = RUNS / "state"
    if restore:
        shared.restore_directory(RUNS / "seed", data)
    data.mkdir(parents=True, exist_ok=True)
    names = {"SECRET_KEY_BASE", "SECRET_KEY_BASE_DUMMY", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "TLS_DOMAIN",
             "RAILS_ENV", "RAILS_MAX_THREADS", "RAILS_LOG_LEVEL", "JOB_CONCURRENCY", "DISABLE_SSL", "HTTP_PORT", "TARGET_PORT", "TARGET_BIND"}
    env = {k: v for k, v in os.environ.items() if k not in names and not k.startswith(("CAMPFIRE_", "THRUSTER_", "TOKIO_"))}
    env.update(configuration(cached))
    env.update({"SECRET_KEY_BASE": hashlib.sha512(b"celld-campfire-bench disposable local fixture").hexdigest(),
                "CAMPFIRE_STORAGE_PATH": str(data), "THRUSTER_HTTP_PORT": str(PORT),
                "THRUSTER_TARGET_PORT": str(TARGET_PORT), "TARGET_BIND": "127.0.0.1"})
    log_path = RUNS / f"{label}.log"
    with log_path.open("w+") as log:
        started = time.perf_counter()
        process = subprocess.Popen([str(BINARY), "server"], cwd=RUNS, env=env, stdout=log, stderr=log)
        connection = None
        try:
            for _ in range(20000):
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
                try:
                    status, _, body = request(connection, "GET", "/up")
                    if status == 200 and b"background-color: green" in body:
                        break
                    raise RuntimeError(f"Unexpected native health response: {status} {body[:100]!r}")
                except (OSError, http.client.HTTPException):
                    connection.close()
                time.sleep(.001 if label.startswith("cold-") else .01)
            else:
                raise RuntimeError("Basecamp application did not become ready")
            yield {"process": process, "connection": connection, "data": data, "base": f"http://127.0.0.1:{PORT}",
                   "spawned_at": started, "ready_at": time.perf_counter()}
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError("Basecamp application did not stop cleanly")
            if process.returncode:
                log.seek(0)
                raise RuntimeError(f"Basecamp application exited with {process.returncode}\n{log.read()}")


def database(data):
    import sqlite3
    return sqlite3.connect(data / "db/production.sqlite3")


def seed_html(room, message):
    return "<p>" + html.escape(seed_body(room, message), quote=False).replace("\n", "<br>") + "</p>"


def sql_time(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def prepare():
    data = RUNS / "state"
    if data.exists():
        shutil.rmtree(data)
    with server("prepare-schema", restore=False):
        pass
    with database(data) as db:
        at = "2026-10-21 08:00:00.000000"
        password_source = (SOURCE / "parity/seeds/lib/seed.rb").read_text()
        password_digest = re.search(r'PASSWORD_DIGEST = "([^"]+)"', password_source)[1]
        db.execute("INSERT INTO accounts(id,created_at,join_code,name,settings,updated_at) VALUES(1,?,'benchmark','Workers Chat Benchmark','{}',?)", (at, at))
        for user in range(1, 9):
            db.execute("INSERT INTO users(id,created_at,email_address,name,password_digest,role,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (user, at, f"user{user}@example.test", user_name(user), password_digest, int(user == 1), at))
        for room in range(1, 51):
            db.execute("INSERT INTO rooms(id,created_at,creator_id,name,type,updated_at) VALUES(?,?,1,?,'Rooms::Open',?)", (room, at, f"Room {room}", at))
            for user in range(1, 8):
                unread_at = sql_time(1699900000 + 1000 - initial_unread(room, user))
                db.execute("INSERT INTO memberships(created_at,room_id,unread_at,updated_at,user_id) VALUES(?,?,?,?,?)", (at, room, unread_at, at, user))
            for message in range(1, 1001):
                identity = (room - 1) * 1000 + message
                created = sql_time(1699900000 + message)
                db.execute("INSERT INTO messages(id,client_message_id,created_at,creator_id,room_id,updated_at) VALUES(?,?,?,?,?,?)",
                           (identity, f"seed-{room}-{message}", created, (message - 1) % 7 + 1, room, created))
                db.execute("INSERT INTO action_text_rich_texts(body,created_at,name,record_id,record_type,updated_at) VALUES(?,?,'body',?,'Message',?)",
                           (seed_html(room, message), created, identity, created))
                db.execute("INSERT INTO message_search_index(rowid,body) VALUES(?,?)", (identity, seed_body(room, message)))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cookies = []
    with server("prepare-sessions", restore=False) as s:
        for user in range(1, 5):
            body = urllib.parse.urlencode({"email_address": f"user{user}@example.test", "password": "secret123456"}).encode()
            status, headers, _ = request(s["connection"], "POST", "/session", body)
            assert status in (302, 303), (status, headers)
            jar = http.cookies.SimpleCookie()
            for name, value in headers:
                if name.lower() == "set-cookie":
                    jar.load(value)
            assert "session_token" in jar
            cookies.append(jar["session_token"].OutputString(attrs=[]))
    (RUNS / "cookies.json").write_text(json.dumps(cookies) + "\n")
    audit(data, [])
    shared.restore_directory(data, RUNS / "seed")
    print("Prepared original Basecamp schema: 50 rooms, 50,000 messages, and authenticated sessions.", flush=True)


def checked_page(connection, room, user, cookie, *, full_room=False, extra=None):
    path = f"/rooms/{room}" + ("" if full_room else "/messages")
    status, headers, body = request(connection, "GET", path, cookie=cookie)
    assert status == 200 and dict(headers).get("content-type", "").startswith("text/html"), (status, body[:200])
    validate_page(room, user, body, full_room=full_room, extra=extra)
    return body


def validate_page(room, user, body, *, full_room=False, extra=None):
    text = body.decode()
    identities = [int(n) for n in re.findall(r'data-message-id="(\d+)"', text)]
    expected = list(range((room - 1) * 1000 + 961, room * 1000 + 1))
    if extra is not None:
        expected = expected[1:] + [extra]
    assert identities == expected, (room, user, identities, expected)
    assert b"\xe4\xbd\xa0\xe5\xa5\xbd" in body
    plain = html.unescape(re.sub(r"<[^>]*>", "", text.replace("<br>", "\n").replace("<br />", "\n")))
    normalized = " ".join(plain.split())
    for message in range(962 if extra is not None else 961, 1001):
        assert " ".join(seed_body(room, message).split()) in normalized, (room, message)
    if full_room:
        assert text.lower().startswith("<!doctype html>") and text.rstrip().endswith("</html>")


def post(connection, room, cookie, client_id):
    body = urllib.parse.urlencode({"message[body]": POST_HTML, "message[client_message_id]": client_id}).encode()
    status, _, body = request(connection, "POST", f"/rooms/{room}/messages", body, cookie)
    assert status == 200 and b"<turbo-stream" in body and b"New message" in body, (status, body[:200])
    ids = re.findall(rb'data-message-id="(\d+)"', body)
    assert len(ids) == 1 and f'id="message_{client_id}"'.encode() in body
    return {"id": int(ids[0]), "room": room, "client_message_id": client_id}


def audit(data, receipts):
    with database(data) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM rooms").fetchone() == (50,)
        assert db.execute("SELECT count(*) FROM users").fetchone() == (8,)
        expected_count = 50000 + len(receipts)
        for table in ("messages", "action_text_rich_texts", "message_search_index"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (expected_count,), table
        assert len({r["id"] for r in receipts}) == len(receipts)
        assert len({r["client_message_id"] for r in receipts}) == len(receipts)
        for receipt in receipts:
            row = db.execute("SELECT m.room_id,m.creator_id,m.client_message_id,r.body,i.body FROM messages m JOIN action_text_rich_texts r ON r.record_id=m.id AND r.record_type='Message' JOIN message_search_index i ON i.rowid=m.id WHERE m.id=?", (receipt["id"],)).fetchone()
            assert row == (receipt["room"], 1, receipt["client_message_id"], POST_HTML, POST_TEXT.strip()), (receipt, row)
        return {"integrity": "ok", "rooms": 50, "seed_messages": 50000, "posts": len(receipts),
                "acknowledged_posts_checked": len(receipts), "search_rows": expected_count}


def provenance():
    build = check_build()
    paths = {ROOT / p for p in build["sources"]} | {OUTPUT / "build.json", BINARY, LOAD}
    hashes = {}
    for path in sorted(paths):
        key = str(path.relative_to(ROOT))
        hashes[key] = digest(path)
        if path not in (BINARY, LOAD):
            target = RUNS / "sources" / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return {"basecamp": build["basecamp"], "vips": build["vips"], "vips_features": build["vips_features"], "glib": build["glib"],
            "sources": hashes, "source_snapshot": "sources",
            "recorded_only": {str(p.relative_to(ROOT)): "Measured binary digest; rebuild from source." for p in (BINARY, LOAD)},
            "seed_sha256": {str(p.relative_to(RUNS / "seed")): digest(p) for p in sorted((RUNS / "seed").rglob("*")) if p.is_file()}}


def validate():
    cookies = json.loads((RUNS / "cookies.json").read_text())
    results = []
    for cached in (False, True):
        receipts = []
        checks = 0
        with server(f"validate-{cached}", cached=cached) as s:
            for room in range(1, 51):
                for user in range(1, 5):
                    for full in (False, True):
                        checked_page(s["connection"], room, user, cookies[user - 1], full_room=full)
                        checks += 1
            status, _, _ = request(s["connection"], "GET", "/rooms/1/messages")
            assert status == 302
            receipt = post(s["connection"], 1, cookies[0], "validation-post")
            receipts.append(receipt)
            for user in range(1, 5):
                checked_page(s["connection"], 1, user, cookies[user - 1], extra=receipt["id"])
                checks += 1
        before = audit(s["data"], receipts)
        with server(f"validate-restart-{cached}", cached=cached, restore=False) as s:
            checked_page(s["connection"], 1, 1, cookies[0], extra=receipt["id"])
            checks += 1
        results.append({"cached": cached, "response_checks": checks + 2, "before_restart": before,
                        "after_restart": audit(s["data"], receipts)})
    (RUNS / "validation.json").write_text(json.dumps({"complete": True, **provenance(), "cases": results}, indent=2) + "\n")
    print("Validated original application pages, authentication, cache invalidation, SQL writes, and restart persistence.", flush=True)


def timed(case, trial, seconds):
    label = f"{trial}-{case}"
    cookies = json.loads((RUNS / "cookies.json").read_text())
    cached = case in ("messages-cached-html", "post")
    with server(label, cached=cached) as s:
        pages = []
        for room in range(1, 51):
            for user in range(1, 5):
                body = checked_page(s["connection"], room, user, cookies[user - 1], full_room=case == "room-html")
                pages.append({"room": room, "user": user, "length": len(body), "crc32": zlib.crc32(body)})
        for _ in range(9):
            for page in pages:
                path = f"/rooms/{page['room']}" + ("" if case == "room-html" else "/messages")
                status, _, body = request(s["connection"], "GET", path, cookie=cookies[page["user"] - 1])
                assert status == 200 and len(body) == page["length"] and zlib.crc32(body) == page["crc32"]
        receipts = []
        if case == "post":
            for index in range(20):
                for room in range(1, 51):
                    receipts.append(post(s["connection"], room, cookies[0], f"warm-{room}-{index}"))
        manifest = RUNS / f"{label}-client.json"
        manifest.write_text(json.dumps({"base": s["base"], "cookies": cookies, "pages": pages, "post_html": POST_HTML}))
        time.sleep(.2)
        before = usage(s["process"].pid)
        samples = [before]
        child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        outfile = RUNS / f"{label}.json"
        with (RUNS / f"{label}-client.log").open("w+") as log:
            client = subprocess.Popen([str(LOAD), str(manifest), case, "32", str(seconds), str(outfile)], stdout=log, stderr=log)
            try:
                while client.poll() is None:
                    samples.append(usage(s["process"].pid))
                    time.sleep(.1)
                after = usage(s["process"].pid)
                samples.append(after)
                child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                if client.returncode:
                    log.seek(0)
                    raise RuntimeError(log.read())
            finally:
                if client.poll() is None:
                    client.kill()
                    client.wait()
        result = json.loads(outfile.read_text())
        assert not result["errors"]
        receipts += result.pop("receipts")
        cpu = after["user_ns"] + after["system_ns"] - before["user_ns"] - before["system_ns"]
        client_cpu = child_after.ru_utime + child_after.ru_stime - child_before.ru_utime - child_before.ru_stime
        result.update({"engine": "basecamp-original", "case": case, "trial": trial, "concurrency": 32,
                       "configuration": configuration(cached), "cpu_ns": cpu,
                       "cpu_us_per_operation": cpu / result["successful"] / 1000,
                       "cpu_percent_one_core": cpu / 1e9 / result["elapsed_s"] * 100,
                       "client_cpu_percent_one_core": client_cpu / result["elapsed_s"] * 100,
                       "max_footprint_bytes": max(v["footprint_bytes"] for v in samples),
                       "max_rss_bytes": max(v["rss_bytes"] for v in samples), "memory_samples": samples,
                       "warmup_reads": 2000, "warmup_writes": 1000 if case == "post" else 0})
    result["database_audit"] = audit(s["data"], receipts)
    print(f"Basecamp {case:22} round={trial} {result['requests_per_s']:.0f}/s CPU={result['cpu_us_per_operation']:.2f}us memory={result['max_footprint_bytes']/2**20:.1f}MiB", flush=True)
    return result


def machine():
    return {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "platform": platform.platform(),
            "cpu": subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip(),
            "memory_bytes": int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)),
            "logical_cpus": int(subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"], text=True)),
            "mach_timebase": {"numer": TIMEBASE.numer, "denom": TIMEBASE.denom},
            "thermal_before": subprocess.check_output(["pmset", "-g", "therm"], text=True)}


def benchmark(seconds, rounds, compare):
    metadata = machine()
    cohort = metadata["started_at"]
    result = {"complete": False, "cohort": cohort, **metadata, **provenance(), "seconds": seconds, "rounds": rounds, "trials": []}
    if compare:
        workerd.shared.make_oracle(workerd.RUNS / "oracle.json", versions=max(32, 1 + int(seconds * 100 / 50 + 1)))
        other = {"complete": False, "cohort": cohort, **metadata, **workerd.provenance(),
                 "engines": workerd.ENGINES, "seconds": seconds, "rounds": rounds, "trials": []}
    for trial in range(rounds):
        if compare:
            engines = workerd.ENGINES + ["basecamp-original"]
            order = engines[trial % len(engines):] + engines[:trial % len(engines)]
            mapping = {"uncached-html": "messages-html", "cached-html": "messages-cached-html", "post": "post"}
            for case in shared.CASES:
                for engine in order:
                    if engine == "basecamp-original":
                        if case in mapping:
                            result["trials"].append(timed(mapping[case], trial, seconds))
                    else:
                        other["trials"].append(shared.timed(engine, case, 32, trial, seconds, server_factory=workerd.server, run_directory=workerd.RUNS))
                    (RUNS / "results.json").write_text(json.dumps(result, indent=2) + "\n")
                    (workerd.RUNS / "results.json").write_text(json.dumps(other, indent=2) + "\n")
            for rooms, listeners in ((1, 32), (5, 8)):
                for engine in order:
                    if engine != "basecamp-original":
                        other["trials"].append(shared.timed(engine, f"ws-{rooms}x{listeners}", rooms * (listeners + 1), trial, seconds, rooms, listeners, server_factory=workerd.server, run_directory=workerd.RUNS))
            result["trials"].append(timed("room-html", trial, seconds))
        else:
            for case in CASES:
                result["trials"].append(timed(case, trial, seconds))
        (RUNS / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    ending = {"complete": True, "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "thermal_after": subprocess.check_output(["pmset", "-g", "therm"], text=True)}
    result.update(ending)
    (RUNS / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    if compare:
        other.update(ending)
        (workerd.RUNS / "results.json").write_text(json.dumps(other, indent=2) + "\n")


def cold(rounds, compare):
    result = {"complete": False, **provenance(), "rounds": rounds, "trials": []}
    cookies = json.loads((RUNS / "cookies.json").read_text())
    for trial in range(rounds):
        with server(f"cold-{trial}") as s:
            status, _, body = request(s["connection"], "GET", "/rooms/1/messages", cookie=cookies[0])
            end = time.perf_counter()
            memory = usage(s["process"].pid)
            assert status == 200
            validate_page(1, 1, body)
            result["trials"].append({"trial": trial, "process_to_first_page_ms": (end - s["spawned_at"]) * 1000,
                                     "cpu_ms": (memory["user_ns"] + memory["system_ns"]) / 1e6, "memory": memory})
        print(f"Cold Basecamp round={trial}: {result['trials'][-1]['process_to_first_page_ms']:.2f} ms", flush=True)
    result["complete"] = True
    (RUNS / "cold-results.json").write_text(json.dumps(result, indent=2) + "\n")
    if compare:
        workerd.cold(rounds, workerd.ENGINES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "validate", "bench", "cold", "all", "compare"])
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--cold-rounds", type=int, default=10)
    args = parser.parse_args()
    compare = args.action == "compare"
    check_build()
    if compare:
        workerd.check_build()
        workerd.prepare()
        workerd.validate(workerd.ENGINES)
    if args.action in ("prepare", "all", "compare"):
        prepare()
    if args.action in ("validate", "all", "compare"):
        validate()
    if args.action in ("bench", "all", "compare"):
        benchmark(args.seconds, args.rounds, compare)
    if args.action in ("cold", "all", "compare"):
        cold(args.cold_rounds, compare)


if __name__ == "__main__":
    main()

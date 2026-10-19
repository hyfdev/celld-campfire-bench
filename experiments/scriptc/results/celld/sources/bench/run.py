import argparse
import contextlib
import hashlib
import http.client
import json
import math
import os
import pathlib
import platform
import resource
import selectors
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time

from build import BINARIES, CELLD_SOURCE, CELLD, GUEST, LOAD, WS, check_build
from fixture import (ACTIVE_USERS, POST_TEXT, POST_JSON, ROOMS, baseline_page,
                     page_bytes, post_bytes, make_oracle, initial_unread, message_row, user_name)
from metrics import usage, TIMEBASE

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = CELLD_SOURCE
RUNS = ROOT / ".runs/current"
PROJECT = RUNS / "app"
STATE = PROJECT / ".celld/dev"
ENGINES = ["js", "wasm", "native", "wasm-v8", "wasm-js"]
CASES = ["uncached-html", "uncached-json", "cached-html", "post", "mixed"]
WS_TEXT = "WebSocket pressure: 你好 👋 <b>& text</b>"


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


ESBUILD = str(ROOT / "node_modules/.bin/esbuild")


def env(engine):
    values = {k: v for k, v in os.environ.items() if not k.startswith("CELLD_")}
    values.pop("WORKERS_CHAT_COMPILE_CACHE", None)
    values.update({"CELLD_ESBUILD": ESBUILD, "CELLD_TOKIO_THREADS": "1", "CELLD_MAX_STATELESS_ISOLATES": "1", "CELLD_MAX_CELL_REQUESTS": "256",
                   "CELLD_NODE": "workers-chat-local", "CELLD_INTERNAL_DEV_STORE": str(STATE / "objects.sqlite3"),
                   "CELLD_WATCH": str(STATE / "runtime"), "WORKERS_CHAT_ENGINE": "wasm" if engine == "wasm-cached" else engine,
                   "WORKERS_CHAT_WASM": str(GUEST), "RUST_LOG": "error"})
    if engine == "wasm-cached":
        values["WORKERS_CHAT_COMPILE_CACHE"] = str(RUNS / "wasmtime-cache")
    for key in ("AWS_DEFAULT_REGION", "AWS_REGION", "S3_ENDPOINT"):
        values.pop(key, None)
    return values


def request(c, method, path, body=b"", user=1, cache=False, authorization=None):
    headers = {"content-type": "application/json", "x-chat-cache": str(int(cache)),
               "authorization": f"Bearer token-{user}" if authorization is None else authorization}
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, data


def checked_read(c, room, user=1, json_response=False, posts=(), cache=False, hit=None, extra_versions=0, created_versions=None):
    status, headers, data = request(c, "GET", f"/rooms/{room}" + ("/messages" if json_response else ""), user=user, cache=cache)
    expected = page_bytes(room, user, json_response, posts) if posts else baseline_page(room, user, json_response)
    if extra_versions or created_versions is not None:
        assert json_response
        page = json.loads(expected)
        page["version"] += extra_versions
        for message in page["messages"]:
            if message["id"] > 1000:
                message["created_at"] = 1700000000 + created_versions[message["id"] - 1001]
        expected = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()
    assert status == 200 and data == expected, (room, status, data[:200], expected[:200])
    assert int(headers["x-chat-version"]) == len(posts) + 1 + extra_versions
    actual_hit = headers["x-chat-cache-hit"] == "1"
    assert int(headers["x-chat-sql-calls"]) == (3 if actual_hit else 4)
    if hit is not None:
        assert actual_hit == hit, (room, user, json_response, actual_hit, hit)
    return headers


def checked_post(c, room, user, text, version):
    data = json.dumps({"body": text}, ensure_ascii=False, separators=(",", ":")).encode()
    status, headers, body = request(c, "POST", f"/rooms/{room}/messages", data, user)
    assert status == 201 and body == post_bytes(room, user, text, version), (status, body[:200])
    assert int(headers["x-chat-version"]) == version and headers["x-chat-cache-hit"] == "0"
    assert int(headers["x-chat-sql-calls"]) == 4


def restore_directory(source, target):
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


@contextlib.contextmanager
def server(engine, label, restore=True, bootstrap=False):
    RUNS.mkdir(parents=True, exist_ok=True)
    for port in (19878, 19879):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            probe.listen(1)
    data = STATE
    if restore:
        restore_directory(RUNS / ("seed-wasm-js" if engine == "wasm-js" else "seed-celld"), data)
    if bootstrap:
        command = [str(CELLD), "dev", str(PROJECT), "--port", "19878", "--no-watch"]
        port = 19878
    else:
        command = [str(CELLD), "--no-control-plane", "--bucket", "celld-dev", "--listen", "127.0.0.1:19878", "--internal-listen", "127.0.0.1:19879"]
        port = 19878
    log_path = RUNS / f"{label}.log"
    with log_path.open("w+") as log:
        start = time.perf_counter()
        process = subprocess.Popen(command, env=env(engine), cwd=REPO, stdout=log, stderr=log)
        c = None
        service_ready = False
        try:
            cold_start = label.startswith("cold-")
            for _ in range(12000 if cold_start else 600):
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    status, _, body = request(c, "GET", "/")
                    if status == 200 and body == (ROOT / "application/index.html").read_bytes():
                        break
                except (OSError, http.client.HTTPException):
                    c.close()
                time.sleep(0.001 if cold_start else 0.02)
            else:
                raise RuntimeError("server did not become ready")
            ready = time.perf_counter()
            service_ready = True
            yield {"process": process, "connection": c, "base": f"http://127.0.0.1:{port}", "data": data,
                   "spawned_at": start, "ready_at": ready, "log": log_path}
        finally:
            if c:
                c.close()
            if process.poll() is None:
                if not bootstrap and service_ready:
                    try:
                        shutdown = http.client.HTTPConnection("127.0.0.1", 19879, timeout=5)
                        request(shutdown, "POST", "/shutdown?handoff=preserve")
                        shutdown.close()
                    except (OSError, http.client.HTTPException):
                        process.send_signal(signal.SIGTERM)
                else:
                    process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError(f"server did not shut down cleanly: {label}")
            assert process.returncode in (0, -signal.SIGTERM), (label, process.returncode, log_path)


def audit(data, posts, read_resets=None, created_versions=None):
    read_resets = read_resets or {}
    databases = {}
    for path in data.rglob("*.sqlite"):
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                first = db.execute("SELECT body FROM messages WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            continue
        if not first:
            continue
        room = int(first[0].split(",", 1)[0].split()[1])
        if room in databases:
            raise AssertionError(f"multiple live databases for room {room}: {databases[room]}, {path}")
        databases[room] = path
    assert set(databases) == set(range(1, 51)), (len(databases), list(data.iterdir()))
    total = 0
    for room, path in databases.items():
        expected_posts = posts.get(room, ())
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("SELECT count(*),min(id),max(id) FROM messages").fetchone() == (1000+len(expected_posts), 1, 1000+len(expected_posts))
            assert db.execute("SELECT version FROM room_meta").fetchone() == (1+len(expected_posts)+read_resets.get(room, (0, 0))[0],)
            reset_users = read_resets.get(room, (0, set()))[1]
            if isinstance(reset_users, int):
                reset_users = {reset_users}
            assert db.execute("SELECT user_id, unread FROM members ORDER BY user_id").fetchall() == [(u, 0 if u in reset_users else initial_unread(room, u)+sum(sender != u for sender, _ in expected_posts)) for u in range(1, 8)]
            assert db.execute("SELECT id,name,token FROM users ORDER BY id").fetchall() == [(u, user_name(u), f"token-{u}") for u in range(1,9)]
            for row in db.execute("SELECT id,user_id,body,created_at FROM messages ORDER BY id"):
                m = message_row(room, row[0], expected_posts)
                if row[0] > 1000 and created_versions is not None and room in created_versions:
                    m["created_at"] = 1700000000 + created_versions[room][row[0] - 1001]
                assert row == (m["id"], m["user_id"], m["body"], m["created_at"]), (room, row[0])
        total += len(expected_posts)
    return {"rooms": 50, "seed_messages": 50000, "posts": total, "integrity": "ok"}


def prepare():
    PROJECT.mkdir(parents=True, exist_ok=True)
    for name in ("worker.js", "wasm-worker.js", "wrangler.jsonc"):
        shutil.copy2(ROOT / "application" / name, PROJECT / name)
    shutil.copy2(GUEST, PROJECT / "chat.wasm")
    if STATE.exists():
        shutil.rmtree(STATE)
    with server("js", "prepare-deployment", restore=False, bootstrap=True):
        pass
    with server("js", "prepare-celld", restore=False) as s:
        for room in range(1, 51):
            checked_read(s["connection"], room, json_response=True)
    assert audit(STATE, {})["posts"] == 0
    restore_directory(STATE, RUNS / "seed-celld")
    config = json.loads((PROJECT / "wrangler.jsonc").read_text())
    config["main"] = "wasm-worker.js"
    (PROJECT / "wrangler.jsonc").write_text(json.dumps(config, indent=2) + "\n")
    try:
        with server("wasm-js", "prepare-wasm-js-deployment", restore=False, bootstrap=True):
            pass
        with server("wasm-js", "prepare-wasm-js", restore=False) as s:
            for room in range(1, 51):
                checked_read(s["connection"], room, json_response=True)
        assert audit(STATE, {})["posts"] == 0
        restore_directory(STATE, RUNS / "seed-wasm-js")
    finally:
        shutil.copy2(ROOT / "application/wrangler.jsonc", PROJECT / "wrangler.jsonc")
    (RUNS / "baseline.json").write_text(json.dumps({"sources": sources(), "datasets": "50 rooms, 1000 messages each"}, indent=2)+"\n")
    print("Prepared and audited the persistent dataset", flush=True)


def warm(s, case):
    cache = case.startswith("cached") or case == "mixed"
    for _ in range(5):
        for room in range(1, 51):
            for user in range(1, 5):
                for j in (False, True):
                    checked_read(s["connection"], room, user, j, cache=cache)
    writes = 20 if case in ("post", "ws") else 0
    for i in range(writes):
        for room in range(1, 51):
            checked_post(s["connection"], room, 1, POST_TEXT, i+2)
    return writes


def validate():
    output = {"complete": False, **provenance(), "engines": []}
    for engine in ENGINES:
        posts = {r: [] for r in range(1, 51)}
        checks = 0
        with server(engine, f"validate-{engine}") as s:
            c = s["connection"]
            status, _, data = request(c, "POST", "/login", b'{"user":1,"password":"password-1"}')
            assert status == 200 and json.loads(data) == {"token":"token-1"}
            status, _, _ = request(c, "POST", "/login", b'{"user":1,"password":"wrong"}')
            assert status == 401
            checks += 2
            for body in (b"null", b"false", b"0", b'"text"', b"[]", b"{}", b"{",
                         b'{"user":"1","password":"password-1"}',
                         b'{"user":1.5,"password":"password-1.5"}'):
                status, _, data = request(c, "POST", "/login", body)
                assert status == 401 and json.loads(data) == {"error":"unauthorized"}, (engine, body, status, data)
                checks += 1
            for room in range(1, 51):
                for user in (1, 4):
                    for j in (False, True):
                        checked_read(c, room, user, j, cache=True)
                        checked_read(c, room, user, j, cache=True, hit=True)
                        checks += 2
            for room in (1,25,50):
                for user,text in ((1,POST_TEXT),(2,'Second user: 你好 👋 <tag> & "quoted" \'single\'\n')):
                    posts[room].append((user,text))
                    checked_post(c,room,user,text,len(posts[room])+1)
                    checks += 1
                    for reader in (1,2,4):
                        for j in (False,True):
                            checked_read(c,room,reader,j,posts[room],cache=True,hit=False)
                            checked_read(c,room,reader,j,posts[room],cache=True,hit=True)
                            checks += 2
            errors = [("GET","/rooms/1",b"","",401),("GET","/rooms/1",b"","Bearer invalid",401),("GET","/rooms/1",b"","Bearer token-8",403),
                      ("GET","/rooms/0",b"","Bearer token-1",404),("GET","/rooms/51",b"","Bearer token-1",404),("GET","/rooms/+1",b"","Bearer token-1",404)]
            for body in (b"{", b"{}", b'{"body":null}', b'{"body":""}', b'{"body":"  \\n"}', json.dumps({"body":"云"*2731},ensure_ascii=False).encode()):
                errors.append(("POST","/rooms/1/messages",body,"Bearer token-1",400))
            for method,path,body,auth,status in errors:
                actual,_,_=request(c,method,path,body,authorization=auth)
                assert actual==status,(engine,path,actual,status)
                checks += 1
            for i in range(50):
                posts[3].append((1,"&"*8192))
                checked_post(c,3,1,"&"*8192,i+2)
                checks += 1
            for j in (False,True):
                checked_read(c,3,1,j,posts[3],cache=True)
                checked_read(c,3,1,j,posts[3],cache=True,hit=j)
                checks += 2
            for user, text in ((1, "\0" * 8192), (2, '\\"\n' * 2730 + "ab"),
                               (1, "\ufeffLeading BOM: 你好 👋 <b>& text</b>")):
                posts[4].append((user, text))
                checked_post(c, 4, user, text, len(posts[4]) + 1)
                checks += 1
                for j in (False, True):
                    checked_read(c, 4, user, j, posts[4])
                    checks += 1
            # Room 1 starts after the two checked HTTP posts.
            wsfile=RUNS/f"validate-{engine}-ws.json"
            subprocess.run([str(WS),s["base"],"1","3","0.5","3",str(wsfile)],check=True,stdout=subprocess.DEVNULL,input="go\n",text=True)
            ws=json.loads(wsfile.read_text())
            posts[1] += [(1,WS_TEXT)]*ws["messages"]
            for room in range(1,51):
                checked_read(c,room,4,True,posts[room])
                checks += 1
        first_audit=audit(s["data"],posts)
        shutil.rmtree(STATE / "runtime")
        (STATE / "runtime").mkdir()
        with server(engine,f"validate-restart-{engine}",restore=False) as s:
            for room in range(1,51):
                for j in (False,True):
                    checked_read(s["connection"],room,4,j,posts[room],cache=True)
                    checks += 1
            status,headers,body=request(s["connection"],"POST","/rooms/2/read",user=2)
            assert status==200 and body==b'{"ok":true}'
            status,_,body=request(s["connection"],"GET","/rooms/2/messages",user=2)
            assert status==200 and json.loads(body)["unread"]==0
            checks += 2
        second_audit=audit(s["data"],posts,{2:(1,2)})
        output["engines"].append({"engine":engine,"response_checks":checks,"websocket":ws,"before_restart":first_audit,"after_restart":second_audit,"restart_source":"object store with runtime directory removed"})
        (RUNS/"validation.json").write_text(json.dumps(output,indent=2)+"\n")
        print(f"Validated {engine}: {checks} HTTP responses, ordered broadcasts, restart and unread persistence",flush=True)
    output["complete"]=True
    (RUNS/"validation.json").write_text(json.dumps(output,indent=2)+"\n")


def timed(engine,case,concurrency,trial,seconds,rooms=None,listeners=None):
    label=f"{trial}-{case}-{concurrency}-{engine}"
    with server(engine,label) as s:
        ready_usage=usage(s["process"].pid)
        writes=warm(s,"ws" if rooms else case)
        time.sleep(.2)
        before=usage(s["process"].pid)
        child_before=resource.getrusage(resource.RUSAGE_CHILDREN)
        samples=[before]
        outfile=RUNS/f"{label}.json"
        if rooms:
            command=[str(WS),s["base"],str(rooms),str(listeners),str(seconds),str(1+writes),str(outfile),"read-acks"]
        else:
            command=[str(LOAD),s["base"],case,str(concurrency),str(seconds),str(1+writes),str(RUNS/"oracle.json"),str(outfile)]
        with (RUNS/f"{label}-client.log").open("w+") as log:
            load=subprocess.Popen(command,stdout=subprocess.PIPE if rooms else log,stderr=log,stdin=subprocess.PIPE if rooms else None,text=True)
            try:
                if rooms:
                    with selectors.DefaultSelector() as selector:
                        selector.register(load.stdout,selectors.EVENT_READ)
                        if not selector.select(timeout=30):
                            raise RuntimeError("WebSocket connections did not become ready")
                    ready = load.stdout.readline()
                    if not ready:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    assert json.loads(ready)=={"ready":True}
                    before=usage(s["process"].pid)
                    samples=[before]
                    child_before=resource.getrusage(resource.RUSAGE_CHILDREN)
                    load.stdin.write("go\n");load.stdin.flush()
                while load.poll() is None:
                    samples.append(usage(s["process"].pid))
                    time.sleep(.1)
                after=usage(s["process"].pid);samples.append(after)
                child_after=resource.getrusage(resource.RUSAGE_CHILDREN)
                if load.returncode:
                    log.seek(0);raise RuntimeError(log.read())
            finally:
                if load.poll() is None:
                    load.kill();load.wait()
                if rooms:
                    load.stdin.close();load.stdout.close()
        result=json.loads(outfile.read_text())
        assert not result["errors"]
        posts={room:[(1,POST_TEXT)]*writes for room in range(1,51)}
        read_resets = {}
        created_versions = {}
        for room,count in enumerate(result["write_counts"],1):
            posts[room]+=[(1,WS_TEXT if rooms else POST_TEXT)]*count
            if rooms:
                participants = listeners + 1
                assert result["read_acknowledgments_enabled"] and result["read_ack_counts"][room-1] == count * participants
                assert result["setup_read_ack_counts"][room-1] == participants
                read_resets[room] = (participants * (count + 1), {1, 2})
                created_versions[room] = list(range(1, writes + 1)) + [1 + writes + participants + i * (participants + 1) for i in range(count)]
        for room in range(1,51):
            checked_read(s["connection"],room,4,True,posts[room],extra_versions=read_resets.get(room,(0,set()))[0],created_versions=created_versions.get(room))
        count=result["messages"] if rooms else result["successful"]
        cpu=after["user_ns"]+after["system_ns"]-before["user_ns"]-before["system_ns"]
        client_cpu=child_after.ru_utime+child_after.ru_stime-child_before.ru_utime-child_before.ru_stime
        result.update({"engine":engine,"case":case,"concurrency":concurrency,"trial":trial,"cpu_ns":cpu,"cpu_us_per_operation":cpu/count/1000,
                       "cpu_percent_one_core":cpu/1e9/result["elapsed_s"]*100,"client_cpu_percent_one_core":client_cpu/result["elapsed_s"]*100,
                       "ready_usage":ready_usage,"warm_usage":before,"end_usage":after,"max_footprint_bytes":max(v["footprint_bytes"] for v in samples),
                       "max_rss_bytes":max(v["rss_bytes"] for v in samples),"memory_samples":samples,"warmup_reads":2000,"warmup_writes":writes*50})
    result["database_audit"]=audit(s["data"],posts,read_resets,created_versions)
    rate=result["messages_per_s"] if rooms else result["requests_per_s"]
    print(f"{engine:10} {case:14} c={concurrency:3} round={trial} {rate:.0f}/s CPU={result['cpu_us_per_operation']:.2f}us memory={result['max_footprint_bytes']/2**20:.1f}MiB p99={result['p99_ms']:.2f}ms",flush=True)
    return result


def sources():
    paths=BINARIES + [ROOT/name for name in (
        "host.rs", "experiments/scriptc/celld/host.rs", "app/src/lib.rs", "app/sql.json", "app/schema.json",
        "application/worker.js", "application/wasm-worker.js", "application/index.html", "application/wrangler.jsonc",
        "guest/src/lib.rs", "guest/.cargo/config.toml", "load/src/main.rs", "load/src/ws.rs",
        "app/Cargo.toml", "app/Cargo.lock", "guest/Cargo.toml", "guest/Cargo.lock",
        "load/Cargo.toml", "load/Cargo.lock", "bench/run.py", "bench/fixture.py",
        "bench/metrics.py", "bench/build.py", "celld.json", "patches/celld.patch",
        "rust-toolchain.toml", "package.json", "pnpm-lock.yaml", ".build/build.json",
    )] + [REPO/name for name in (
        "crates/celld/js.rs", "crates/celld/runtime.rs", "crates/celld/lib.rs",
        "Cargo.toml", "crates/celld/Cargo.toml", "Cargo.lock",
    )]
    return {str(p.relative_to(ROOT)):digest(p) for p in paths}


def provenance():
    hashes=sources()
    for path in hashes:
        if ROOT/path in BINARIES:
            continue
        target=RUNS/"sources"/path
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/path,target)
    return {"sources":hashes,"source_snapshot":"sources",
            "recorded_only":{str(p.relative_to(ROOT)):"Measured binary digest; rebuild from source." for p in BINARIES}}


def benchmark(seconds,rounds,concurrencies):
    make_oracle(RUNS / "oracle.json", versions=max(32, 1 + math.ceil(seconds * 100 / ROOMS)))
    result={"complete":False,"started_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"platform":platform.platform(),"cpu":subprocess.check_output(["sysctl","-n","machdep.cpu.brand_string"],text=True).strip(),
            "celld_base":subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip(),"thermal_before":subprocess.check_output(["pmset","-g","therm"],text=True),
            "celld_tokio_threads":1,"celld_max_cell_requests":256,"rustc":subprocess.check_output(["rustc","--version"],text=True).strip(),
            "memory_bytes":int(subprocess.check_output(["sysctl","-n","hw.memsize"],text=True)),
            "logical_cpus":int(subprocess.check_output(["sysctl","-n","hw.logicalcpu"],text=True)),
            "mach_timebase":{"numer":TIMEBASE.numer,"denom":TIMEBASE.denom},**provenance(),"trials":[]}
    for trial in range(rounds):
        order=ENGINES[trial%len(ENGINES):]+ENGINES[:trial%len(ENGINES)]
        for case in CASES:
            for concurrency in concurrencies:
                for engine in order:
                    result["trials"].append(timed(engine,case,concurrency,trial,seconds))
                    (RUNS/"results.json").write_text(json.dumps(result,indent=2)+"\n")
        for rooms,listeners in ((1,32),(5,8)):
            for engine in order:
                result["trials"].append(timed(engine,f"ws-{rooms}x{listeners}",rooms*(listeners+1),trial,seconds,rooms,listeners))
                (RUNS/"results.json").write_text(json.dumps(result,indent=2)+"\n")
    result["complete"]=True
    result["completed_at"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    result["thermal_after"]=subprocess.check_output(["pmset","-g","therm"],text=True)
    (RUNS/"results.json").write_text(json.dumps(result,indent=2)+"\n")


def cold(rounds):
    result={"complete":False,**provenance(),"trials":[]}
    for trial in range(rounds):
        order=ENGINES[trial%len(ENGINES):]+ENGINES[:trial%len(ENGINES)]
        for engine in order:
            with server(engine,f"cold-{trial}-{engine}") as s:
                before=time.perf_counter()
                checked_read(s["connection"],1,1,False)
                end=time.perf_counter()
                result["trials"].append({"engine":engine,"trial":trial,"process_to_root_ms":(s["ready_at"]-s["spawned_at"])*1000,"first_room_ms":(end-before)*1000,"process_to_first_page_ms":(end-s["spawned_at"])*1000,"memory":usage(s["process"].pid)})
            print(f"Cold {engine} round={trial}: {result['trials'][-1]['process_to_first_page_ms']:.2f}ms",flush=True)
            (RUNS/"cold-results.json").write_text(json.dumps(result,indent=2)+"\n")
    result["complete"]=True
    (RUNS/"cold-results.json").write_text(json.dumps(result,indent=2)+"\n")


def cache_startup_trial(engine, label):
    with server(engine, label) as s:
        before = time.perf_counter()
        checked_read(s["connection"], 1, 1, False, hit=False)
        end = time.perf_counter()
        memory = usage(s["process"].pid)
        events = [json.loads(line.split("CHAT_WASMTIME_MODULE ", 1)[1])
                  for line in s["log"].read_text().splitlines()
                  if line.startswith("CHAT_WASMTIME_MODULE ")]
        assert len(events) == int(engine in ("wasm", "wasm-cached")), events
        return {"engine": engine,
                "process_to_root_ms": (s["ready_at"] - s["spawned_at"]) * 1000,
                "first_room_ms": (end - before) * 1000,
                "process_to_first_page_ms": (end - s["spawned_at"]) * 1000,
                "cpu_ms": (memory["user_ns"] + memory["system_ns"]) / 1e6,
                "memory": memory, "wasmtime": events[0] if events else None,
                "root_validated": True, "first_page_validated": True}


def cold_cache(rounds):
    global RUNS, PROJECT, STATE
    previous = RUNS, PROJECT, STATE
    RUNS = RUNS / "compile-cache"
    PROJECT = RUNS / "app"
    STATE = PROJECT / ".celld/dev"
    try:
        prepare()
        cache_directory = RUNS / "wasmtime-cache"
        if cache_directory.exists():
            shutil.rmtree(cache_directory)
        result = {"complete": False, **provenance(),
                  "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "platform": platform.platform(),
                  "cpu": subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip(),
                  "memory_bytes": int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)),
                  "logical_cpus": int(subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"], text=True)),
                  "mach_timebase": {"numer": TIMEBASE.numer, "denom": TIMEBASE.denom},
                  "cache": "Wasmtime 49 built-in filesystem compilation cache",
                  "rounds": rounds, "trials": []}
        population = cache_startup_trial("wasm-cached", "cold-cache-populate")
        assert population["wasmtime"]["cache_enabled"]
        assert population["wasmtime"]["cache_hits"] == 0 and population["wasmtime"]["cache_misses"] == 1
        result["cache_population"] = population
        engines = ["js", "wasm", "wasm-cached", "native", "wasm-v8", "wasm-js"]
        for trial in range(rounds):
            order = engines[trial % len(engines):] + engines[:trial % len(engines)]
            for engine in order:
                row = cache_startup_trial(engine, f"cold-cache-{trial}-{engine}")
                if engine == "wasm-cached":
                    assert row["wasmtime"]["cache_enabled"]
                    assert row["wasmtime"]["cache_hits"] == 1 and row["wasmtime"]["cache_misses"] == 0, row
                elif engine == "wasm":
                    assert not row["wasmtime"]["cache_enabled"]
                    assert row["wasmtime"]["cache_hits"] == row["wasmtime"]["cache_misses"] == 0
                row["trial"] = trial
                result["trials"].append(row)
                (RUNS / "cold-cache-results.json").write_text(json.dumps(result, indent=2) + "\n")
                print(f"Startup {engine:12} round={trial}: {row['process_to_first_page_ms']:.2f}ms CPU={row['cpu_ms']:.2f}ms memory={row['memory']['footprint_bytes']/2**20:.1f}MiB", flush=True)
        result["complete"] = True
        result["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (RUNS / "cold-cache-results.json").write_text(json.dumps(result, indent=2) + "\n")
    finally:
        RUNS, PROJECT, STATE = previous


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--phase",choices=["prepare","validate","bench","cold","cold-cache","all"],default="all")
    parser.add_argument("--seconds",type=float,default=5)
    parser.add_argument("--rounds",type=int,default=3)
    parser.add_argument("--concurrencies",default="1,32,128")
    args=parser.parse_args()
    check_build()
    RUNS.mkdir(parents=True,exist_ok=True)
    if args.phase in ("prepare","all"):prepare()
    if args.phase in ("validate","all"):validate()
    if args.phase in ("bench","all"):benchmark(args.seconds,args.rounds,[int(c)for c in args.concurrencies.split(",")])
    if args.phase in ("cold","all"):cold(10)
    if args.phase in ("cold-cache","all"):cold_cache(10)


if __name__=="__main__":main()

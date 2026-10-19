from pathlib import Path

LABELS = {
    'js': 'JavaScript / V8 (baseline)',
    'wasm-js': 'Rust Wasm / V8 + JS bindings',
    'wasm-v8': 'Rust Wasm / V8 + direct host calls',
    'wasm': 'Rust Wasm / Wasmtime + direct host calls',
    'native': 'Native Rust',
}


def relative(value, baseline, spec):
    return f"{value:{spec}} ({value / baseline:.2f}×)"


def compile_cache_results(compile_cache, raw_link):
    data, rows = compile_cache
    labels = {**LABELS,
              'wasm': 'Rust Wasm / Wasmtime: compile at startup',
              'wasm-cached': 'Rust Wasm / Wasmtime: reuse compiled module'}
    baseline = next(r for r in rows if r['engine'] == 'js')
    wasm = next(r for r in rows if r['engine'] == 'wasm')
    cached = next(r for r in rows if r['engine'] == 'wasm-cached')
    lines = [
        '### Process startup and the Wasm compilation cache', '',
        f"{data['cpu']}, {data['memory_bytes']/2**30:g} GiB RAM, {' '.join(data['platform'].split('-')[:3])}. Medians of {data['rounds']} fresh processes per row, starting from the same room data. The cached Wasmtime row loads machine code saved before timing. Filesystem caches stay warm.", '',
        '| Application runtime | Start → first room response (ms) | CPU time to first room response (ms) | Process memory after first room response (MiB) |',
        '| --- | ---: | ---: | ---: |',
    ]
    for row in rows:
        cells = [relative(row[key], baseline[key], spec) for key, spec in (
            ('process_to_first_page_ms', '.2f'), ('cpu_ms', '.2f'), ('footprint_mib', '.1f'))]
        lines.append('| ' + labels[row['engine']] + ' | ' + ' | '.join(cells) + ' |')
    lines += ['',
        f"Wasm module compilation/loading: **{wasm['module_ms']:.2f} ms → {cached['module_ms']:.2f} ms** when reusing compiled code. All {data['rounds']} cached trials hit the cache. Load tests exclude startup and compilation. [Raw data]({raw_link}).",
    ]
    return '\n'.join(lines)


def render(raw, cold_data, s, compile_cache=None):
    engines = [engine for engine in LABELS if any(r['engine'] == engine for r in s['http'])]

    def http(case, c, engine):
        return next(r for r in s['http'] if (r['case'], r['concurrency'], r['engine']) == (case, c, engine))

    tables = []
    titles = {'uncached-html': 'Server-rendered HTML (without in-memory page caching)',
              'uncached-json': 'Read messages as JSON (no response cache)',
              'cached-html': 'Server-rendered HTML (with in-memory page caching)',
              'post': 'Post a message; reply after saving it',
              'mixed': 'Read message history while posting new messages'}
    for case, title in titles.items():
        tables.append(f'### {title}\n')
        if case == 'mixed':
            tables.append('One additional client attempts 100 posts/s. Requests/s and CPU time/request include both reads and posts.\n')
        clients = 'Reading clients (+1 posting client)' if case == 'mixed' else 'Simultaneous clients'
        tables.append(f'| {clients} | Application runtime | Total requests/s; min–max across trials | CPU time/request (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) | p99 response time (ms) |')
        tables.append('| ---: | --- | ---: | ---: | ---: | ---: | ---: |')
        for r in [r for r in s['http'] if r['case'] == case]:
            baseline = http(case, r['concurrency'], 'js')
            cells = [relative(r['requests_per_s'], baseline['requests_per_s'], ',.0f')
                     + f"<br>{r['throughput_min']:,.0f}–{r['throughput_max']:,.0f}"]
            cells += [relative(r[key], baseline[key], spec) for key, spec in (
                ('cpu_us_per_operation', '.2f'), ('cpu_percent_one_core', '.1f'),
                ('memory_mib', '.1f'), ('p99_ms', '.2f'))]
            tables.append(f"| {r['concurrency']} | {LABELS[r['engine']]} | " + ' | '.join(cells) + ' |')
        if case == 'mixed':
            tables.append('\n| Reading clients (+1 posting client) | Application runtime | Reads/s | Posts/s | Reads served from response cache (%) |')
            tables.append('| ---: | --- | ---: | ---: | ---: |')
            for r in [r for r in s['http'] if r['case'] == case]:
                baseline = http(case, r['concurrency'], 'js')
                cells = [relative(r[key], baseline[key], spec) for key, spec in (
                    ('reads_per_s', ',.0f'), ('writes_per_s', '.1f'), ('cache_hit_percent', '.1f'))]
                tables.append(f"| {r['concurrency']} | {LABELS[r['engine']]} | " + ' | '.join(cells) + ' |')
        tables.append('')

    socket_groups = [
        ('WebSocket chat throughput and server resources',
         '| Rooms and receiving connections | Application runtime | Total messages/s; min–max across trials | CPU time/message (µs) | CPU use (%; 100% = 1 core) | Peak process memory (MiB) |',
         ('messages_per_s', 'cpu_us_per_operation', 'cpu_percent_one_core', 'memory_mib'),
         (',.0f', '.2f', '.1f', '.1f')),
        ('WebSocket deliveries and read confirmations',
         '| Rooms and receiving connections | Application runtime | Message deliveries/s | HTTP read confirmations/s | Send → sender receives message: p99 (ms) | Send → all read confirmations: p99 (ms) |',
         ('deliveries_per_s', 'read_acknowledgments_per_s', 'sender_p99_ms', 'p99_ms'),
         (',.0f', ',.0f', '.2f', '.2f')),
    ]
    for title, header, keys, specs in socket_groups:
        tables += [f'### {title}\n', header, '| --- | --- | ---: | ---: | ---: | ---: |']
        for r in s['websocket']:
            baseline = next(b for b in s['websocket'] if (b['case'], b['concurrency'], b['engine'])
                            == (r['case'], r['concurrency'], 'js'))
            cells = [relative(r[key], baseline[key], spec) for key, spec in zip(keys, specs)]
            if keys[0] == 'messages_per_s':
                cells[0] += f"<br>{r['throughput_min']:,.0f}–{r['throughput_max']:,.0f}"
            setup = {'ws-1x32': '1 room, 32 receiving connections',
                     'ws-5x8': '5 rooms, 8 receiving connections per room'}[r['case']]
            tables.append(f"| {setup} | {LABELS[r['engine']]} | " + ' | '.join(cells) + ' |')
        tables.append('')

    cold = ['| Application runtime | Start → home page response (ms) | Home page → first room response (ms) | Start → first room response (ms); min–max across trials | Process memory after first room response (MiB) |',
            '| --- | ---: | ---: | ---: | ---: |']
    baseline = next(r for r in s['cold'] if r['engine'] == 'js')
    for r in s['cold']:
        cells = [relative(r[key], baseline[key], '.2f') for key in ('process_to_root_ms', 'first_room_ms')]
        cells.append(relative(r['process_to_first_page_ms'], baseline['process_to_first_page_ms'], '.2f')
                     + f"<br>{r['process_to_first_page_ms_min']:.2f}–{r['process_to_first_page_ms_max']:.2f}")
        cells.append(relative(r['footprint_mib'], baseline['footprint_mib'], '.1f'))
        cold.append('| ' + LABELS[r['engine']] + ' | ' + ' | '.join(cells) + ' |')

    checks = s['checks']
    correctness = f"The `results.json`, `cold-results.json`, and `validation.json` records contain **{checks['http_trials']} HTTP trials, {checks['websocket_trials']} WebSocket trials, and {checks['total_cold_trials']} startup trials**, plus **{checks['dedicated_http_checks']:,} dedicated full-response checks**. The HTTP load clients completed {checks['http_requests']:,} requests, including {checks['http_posts']:,} posts. The WebSocket sessions added {checks['websocket_messages']:,} posts, {checks['websocket_deliveries_including_sender']:,} verified message deliveries, and {checks['websocket_read_acknowledgments']:,} verified HTTP read confirmations. All trials passed response and ordering checks; all {checks['database_audits']} post-trial audits passed across all 50 room databases. Source hashes agree across these three records, and captured files match their recorded SHA-256 hashes."
    if compile_cache:
        correctness += f"\n\nThe `compile-cache/` comparison contains **{len(compile_cache[0]['trials'])} additional startup trials**. Its referenced source files match their recorded SHA-256 hashes."
    correctness += f"\n\nThe load tests started at {raw['started_at']} and completed at {raw['completed_at']}. The load-test JSON records the before/after thermal checks. The largest recorded load-client CPU usage was {checks['max_client_cpu_percent_one_core']:.1f}% of one core; clients ran in separate processes on the same machine."

    http_rows = [r for r in raw['trials'] if not r['case'].startswith('ws-')]
    machine = (f"Tests run on {raw['cpu']} with {raw['logical_cpus']} logical CPUs, "
               f"{raw['memory_bytes'] / 2**30:g} GiB RAM, and {' '.join(raw['platform'].split('-')[:3])}. "
               f"The celld base is commit `{raw['celld_base']}`. The compiler is `{raw['rustc']}`.")
    rounds = len({r['trial'] for r in raw['trials']})
    methods = {
        'MACHINE_SETUP': machine,
        'HTTP_IN_FLIGHT': str(max(r['concurrency'] + int(r['case'] == 'mixed') for r in http_rows)),
        'HTTP_CONCURRENCIES': ', '.join(map(str, sorted({r['concurrency'] for r in http_rows}))),
        'TARGET_SECONDS': ', '.join(f"{n:g}" for n in sorted({r['target_seconds'] for r in http_rows})),
        'PRESSURE_ROUNDS': str(rounds),
        'ROUND_SETUP': f"Tests use {rounds} round{'s' if rounds != 1 else ''}. The {len(engines)} runtimes run one at a time; their order rotates between rounds.",
        'STARTUP_ROUNDS': str(len({r['trial'] for r in cold_data['trials']})),
        'MACH_TIMEBASE': f"{raw['mach_timebase']['numer']}/{raw['mach_timebase']['denom']}",
    }
    report = (Path(__file__).parent / 'report-template.md').read_text()
    execution = {
        'js': 'JavaScript in V8 (`v8` crate 152.1.0) | JS Worker and Durable Object APIs',
        'wasm-js': 'Rust Wasm in V8 (`v8` crate 152.1.0) | JS Worker entry and JS bindings',
        'wasm-v8': 'Rust Wasm in V8 (`v8` crate 152.1.0) | Rust callbacks through V8; no JS source execution',
        'wasm': 'Rust Wasm in Wasmtime 49.0.0 | Wasm imports calling Rust directly',
        'native': 'The same Rust source compiled natively | Direct Rust calls',
    }
    execution_table = ['| Application runtime | Application code | SQL and WebSocket access |',
                       '| --- | --- | --- |']
    execution_table += [f'| {LABELS[engine]} | {execution[engine]} |' for engine in engines]
    bridge = ''
    if 'wasm-js' in engines:
        bridge = ('The **V8 + JS bindings** mode imports the same `.wasm` module through celld\'s standard deployment loader. Its [`JS adapter`](../../application/wasm-worker.js) calls the Wasm entry points from JS Worker and Durable Object handlers; Wasm imports call `ctx.storage.sql.exec()`, SQL cursor wrappers, `getWebSockets()`, and per-socket `send()` in JS. SQL result strings are encoded by celld\'s JS TextEncoder before Wasm reads them; the direct paths transfer them as Rust/UTF-8. The V8 pair shares the engine, module, business logic, and binary data format.\n\n')
    for key, text in {'RESULTS_TABLES': '\n'.join(tables),
                      'EXECUTION_TABLE': '\n'.join(execution_table),
                      'JS_BRIDGE_DESCRIPTION': bridge,
                      'COMPILE_CACHE_RESULTS': compile_cache_results(compile_cache, 'compile-cache/cold-cache-results.json') if compile_cache else '',
                      'STARTUP_TABLE': '\n'.join(cold), 'CORRECTNESS_SUMMARY': correctness}.items():
        assert key in report
        report = report.replace(key, text)
    for key, text in methods.items():
        assert key in report
        report = report.replace(key, text)
    return report


def readme_results(summary, compile_cache=None):
    engines = tuple(engine for engine in LABELS if any(r['engine'] == engine for r in summary['http']))
    recorded = 'results/2026-10-16'

    def row(case, engine):
        return next(r for r in summary['http'] if (r['case'], r['concurrency'], r['engine']) == (case, 32, engine))

    columns = '| ' + ' | '.join(LABELS[engine] for engine in engines) + ' |'
    alignment = '| --- | ' + ' | '.join('---:' for _ in engines) + ' |'
    lines = [
        '## Rust → Wasm', '',
        'Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64 · 2026-10-16. Medians of three 5-second trials.', '',
        '### Server-rendered HTML (without in-memory page caching): 32 simultaneous clients', '',
        'Each request authenticates a user, checks room membership, and renders the latest 50 messages. Database and operating-system caches remain active.', '',
        '| Application runtime | Total requests/s | CPU use (%; 100% = 1 core) | CPU time/request (µs) | Peak process memory (MiB) | p99 response time (ms) |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    baseline = row('uncached-html', 'js')
    for engine in engines:
        r = row('uncached-html', engine)
        cells = ['**' + relative(r['requests_per_s'], baseline['requests_per_s'], ',.0f') + '**']
        cells += [relative(r[key], baseline[key], spec) for key, spec in (
            ('cpu_percent_one_core', '.1f'), ('cpu_us_per_operation', '.1f'),
            ('memory_mib', '.1f'), ('p99_ms', '.2f'))]
        lines.append('| ' + LABELS[engine] + ' | ' + ' | '.join(cells) + ' |')
    lines += ['',
        'p99 response time: 99% of requests in a trial finished within this time.', '',
        '### HTTP chat throughput (total requests/s): 32 simultaneous clients', '',
        '| Request ' + columns,
        alignment,
    ]
    for case, label in [('uncached-json', 'Read messages as JSON (no response cache)'),
                        ('cached-html', 'Server-rendered HTML (with in-memory page caching)'),
                        ('post', 'Post a message; reply after saving it')]:
        baseline = row(case, 'js')['requests_per_s']
        lines.append('| ' + label + ' | ' + ' | '.join(relative(row(case, engine)['requests_per_s'], baseline, ',.0f') for engine in engines) + ' |')
    lines += ['',
        '### WebSocket chat throughput (total messages/s)', '',
        "Each room has one sender plus the receiving connections below. The sender waits for storage, broadcast, and every participant's HTTP read confirmation before sending the next message.", '',
        '| Rooms and receiving connections ' + columns,
        alignment,
    ]
    for case, label in [('ws-1x32', '1 room, 32 receiving connections'), ('ws-5x8', '5 rooms, 8 receiving connections per room')]:
        rates = [next(r['messages_per_s'] for r in summary['websocket'] if r['case'] == case and r['engine'] == engine) for engine in engines]
        lines.append('| ' + label + ' | ' + ' | '.join(relative(rate, rates[0], ',.0f') for rate in rates) + ' |')
    lines.append('')
    if compile_cache:
        lines.append(compile_cache_results(compile_cache, f'{recorded}/compile-cache/cold-cache-results.json'))
    else:
        lines += ['### Fresh-process startup', '',
            '| Application runtime | Start → first room response (ms) | Process memory after first room response (MiB) |',
            '| --- | ---: | ---: |']
        baseline = next(r for r in summary['cold'] if r['engine'] == 'js')
        for engine in engines:
            r = next(r for r in summary['cold'] if r['engine'] == engine)
            cells = [relative(r[key], baseline[key], spec) for key, spec in (
                ('process_to_first_page_ms', '.2f'), ('footprint_mib', '.1f'))]
            lines.append('| ' + LABELS[engine] + ' | ' + ' | '.join(cells) + ' |')
        lines += ['',
            'Medians of ten fresh processes. These include service startup, object activation, and the first room response. Filesystem caches stay warm; Wasm compiles in each new process.']
    lines += ['',
        f'[Complete report]({recorded}/REPORT.md) · Raw data: [load tests]({recorded}/results.json), [startup]({recorded}/cold-results.json), [validation]({recorded}/validation.json).',
    ]
    return '\n'.join(lines)

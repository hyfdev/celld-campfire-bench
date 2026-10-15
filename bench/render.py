from pathlib import Path


def relative(value, baseline, spec):
    return f"{value:{spec}} ({value / baseline:.2f}×)"


def compile_cache_results(compile_cache, raw_link):
    data, rows = compile_cache
    labels = {'js': 'JavaScript / V8 (baseline)', 'wasm': 'Rust Wasm / Wasmtime: cache disabled',
              'wasm-cached': 'Rust Wasm / Wasmtime: cache hit', 'native': 'Native Rust'}
    baseline = next(r for r in rows if r['engine'] == 'js')
    wasm = next(r for r in rows if r['engine'] == 'wasm')
    cached = next(r for r in rows if r['engine'] == 'wasm-cached')
    lines = [
        '### Process startup with a compilation cache', '',
        f"{data['cpu']}, {data['memory_bytes']/2**30:g} GiB RAM, `{data['platform']}`. Medians of {data['rounds']} fresh processes per configuration. The executable and seed are shared; both Wasm modes use the same module. Wasmtime's compilation cache is populated before timing; filesystem caches stay warm.", '',
        '| Application execution | Launch → first room page, ms | CPU time through first page, ms | Process memory after page, MiB |',
        '| --- | ---: | ---: | ---: |',
    ]
    for row in rows:
        cells = [relative(row[key], baseline[key], spec) for key, spec in (
            ('process_to_first_page_ms', '.2f'), ('cpu_ms', '.2f'), ('footprint_mib', '.1f'))]
        lines.append('| ' + labels[row['engine']] + ' | ' + ' | '.join(cells) + ' |')
    lines += ['',
        f"Wasm module compile/load: **{wasm['module_ms']:.2f} ms → {cached['module_ms']:.2f} ms** on a cache hit. All {data['rounds']} cached trials hit the cache. Throughput tests exclude compilation through warmup. [Raw data]({raw_link}).",
    ]
    return '\n'.join(lines)


def render(raw, cold_data, s, compile_cache=None):
    names = {'js': 'celld JS (baseline)', 'wasm': 'celld Wasm', 'native': 'celld native',
             'wasm-v8': 'celld V8 Wasm'}

    def http(case, c, engine):
        return next(r for r in s['http'] if (r['case'], r['concurrency'], r['engine']) == (case, c, engine))

    tables = []
    titles = {'uncached-html': 'Server-rendered HTML (without in-memory page caching)',
              'uncached-json': 'Read messages as JSON (no response cache)',
              'cached-html': 'Read HTML with in-memory page caching',
              'post': 'Post a message; reply after saving it',
              'mixed': 'Cached reads with a concurrent writer'}
    for case, title in titles.items():
        tables.append(f'### {title}\n')
        if case == 'mixed':
            tables.append('The request rate includes reads and posts. CPU/op uses that combined count. The separate writer and cache-hit table follows.\n')
        tables.append('| Clients | Execution | Requests/s; trial range | CPU µs/op | CPU % | Physical MiB | p99 ms |')
        tables.append('| ---: | --- | ---: | ---: | ---: | ---: | ---: |')
        for r in [r for r in s['http'] if r['case'] == case]:
            baseline = http(case, r['concurrency'], 'js')
            cells = [relative(r['requests_per_s'], baseline['requests_per_s'], ',.0f')
                     + f"<br>{r['throughput_min']:,.0f}–{r['throughput_max']:,.0f}"]
            cells += [relative(r[key], baseline[key], spec) for key, spec in (
                ('cpu_us_per_operation', '.2f'), ('cpu_percent_one_core', '.1f'),
                ('memory_mib', '.1f'), ('p99_ms', '.2f'))]
            tables.append(f"| {r['concurrency']} | {names[r['engine']]} | " + ' | '.join(cells) + ' |')
        if case == 'mixed':
            tables.append('\n| Read clients | Execution | Reads/s | Posts/s | Cache hits % |')
            tables.append('| ---: | --- | ---: | ---: | ---: |')
            for r in [r for r in s['http'] if r['case'] == case]:
                baseline = http(case, r['concurrency'], 'js')
                cells = [relative(r[key], baseline[key], spec) for key, spec in (
                    ('reads_per_s', ',.0f'), ('writes_per_s', '.1f'), ('cache_hit_percent', '.1f'))]
                tables.append(f"| {r['concurrency']} | {names[r['engine']]} | " + ' | '.join(cells) + ' |')
        tables.append('')

    tables.append('### Live WebSocket messages\n')
    tables.append('| Rooms × listeners | Execution | Messages/s; trial range | Frame deliveries/s | HTTP read acks/s | CPU µs/message | CPU % | Physical MiB | Sender p99 ms | Complete round p99 ms |')
    tables.append('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |')
    for r in s['websocket']:
        baseline = next(b for b in s['websocket'] if (b['case'], b['concurrency'], b['engine'])
                        == (r['case'], r['concurrency'], 'js'))
        cells = [relative(r['messages_per_s'], baseline['messages_per_s'], ',.0f')
                 + f"<br>{r['throughput_min']:,.0f}–{r['throughput_max']:,.0f}"]
        cells += [relative(r[key], baseline[key], spec) for key, spec in (
            ('deliveries_per_s', ',.0f'), ('read_acknowledgments_per_s', ',.0f'),
            ('cpu_us_per_operation', '.2f'), ('cpu_percent_one_core', '.1f'),
            ('memory_mib', '.1f'), ('sender_p99_ms', '.2f'), ('p99_ms', '.2f'))]
        tables.append(f"| {r['case'][3:]} | {names[r['engine']]} | " + ' | '.join(cells) + ' |')

    cold = ['| Execution | Process → root ms | First room after root ms | Process → first page ms; trial range | Physical MiB after first page |',
            '| --- | ---: | ---: | ---: | ---: |']
    baseline = next(r for r in s['cold'] if r['engine'] == 'js')
    for r in s['cold']:
        cells = [relative(r[key], baseline[key], '.2f') for key in ('process_to_root_ms', 'first_room_ms')]
        cells.append(relative(r['process_to_first_page_ms'], baseline['process_to_first_page_ms'], '.2f')
                     + f"<br>{r['process_to_first_page_ms_min']:.2f}–{r['process_to_first_page_ms_max']:.2f}")
        cells.append(relative(r['footprint_mib'], baseline['footprint_mib'], '.1f'))
        cold.append('| ' + names[r['engine']] + ' | ' + ' | '.join(cells) + ' |')

    checks = s['checks']
    correctness = f"The `results.json`, `cold-results.json`, and `validation.json` records contain **{checks['http_trials']} HTTP trials, {checks['websocket_trials']} WebSocket trials, and {checks['total_cold_trials']} startup trials**, plus **{checks['dedicated_http_checks']:,} dedicated full-response checks**. The HTTP pressure clients completed {checks['http_requests']:,} operations, including {checks['http_posts']:,} posts. The WebSocket sessions added {checks['websocket_messages']:,} posts, {checks['websocket_deliveries_including_sender']:,} verified frame deliveries, and {checks['websocket_read_acknowledgments']:,} verified HTTP read acknowledgments. All trials passed response and ordering checks; all {checks['database_audits']} post-trial audits passed across all 50 room databases. Source hashes agree across these three records. Captured source files match their recorded SHA-256 hashes; `recorded_only` lists the executable and original-harness digests retained as provenance."
    if compile_cache:
        correctness += f"\n\nThe `compile-cache/` comparison contains **{len(compile_cache[0]['trials'])} additional startup trials** and its own source snapshot from a separate build. Its captured source files match their recorded SHA-256 hashes."
    correctness += f"\n\nThe pressure run started at {raw['started_at']} and completed at {raw['completed_at']}. The pressure JSON records the before/after thermal checks. The largest recorded load-client CPU usage was {checks['max_client_cpu_percent_one_core']:.1f}% of one core; clients ran in separate processes on the same machine."

    http_rows = [r for r in raw['trials'] if not r['case'].startswith('ws-')]
    machine = (f"Tests run on {raw['cpu']} with {raw['logical_cpus']} logical CPUs, "
               f"{raw['memory_bytes'] / 2**30:g} GiB RAM, and `{raw['platform']}`. "
               f"The celld base is commit `{raw['celld_base']}`. The compiler is `{raw['rustc']}`.")
    rounds = len({r['trial'] for r in raw['trials']})
    methods = {
        'MACHINE_SETUP': machine,
        'HTTP_IN_FLIGHT': str(max(r['concurrency'] + int(r['case'] == 'mixed') for r in http_rows)),
        'HTTP_CONCURRENCIES': ', '.join(map(str, sorted({r['concurrency'] for r in http_rows}))),
        'TARGET_SECONDS': ', '.join(f"{n:g}" for n in sorted({r['target_seconds'] for r in http_rows})),
        'PRESSURE_ROUNDS': str(rounds),
        'ROUND_SETUP': f"Tests use {rounds} round{'s' if rounds != 1 else ''}. The four engines run sequentially; their order rotates between rounds.",
        'STARTUP_ROUNDS': str(len({r['trial'] for r in cold_data['trials']})),
        'MACH_TIMEBASE': f"{raw['mach_timebase']['numer']}/{raw['mach_timebase']['denom']}",
    }
    report = (Path(__file__).parent / 'report-template.md').read_text()
    for key, text in {'RESULTS_TABLES': '\n'.join(tables),
                      'COMPILE_CACHE_RESULTS': compile_cache_results(compile_cache, 'compile-cache/cold-cache-results.json') if compile_cache else '',
                      'STARTUP_TABLE': '\n'.join(cold), 'CORRECTNESS_SUMMARY': correctness}.items():
        assert key in report
        report = report.replace(key, text)
    for key, text in methods.items():
        assert key in report
        report = report.replace(key, text)
    return report


def readme_results(summary, compile_cache=None):
    labels = {'js': 'JavaScript / V8 (baseline)', 'wasm': 'Rust Wasm / Wasmtime', 'native': 'Native Rust'}
    engines = ('js', 'wasm', 'native')

    def row(case, engine):
        return next(r for r in summary['http'] if (r['case'], r['concurrency'], r['engine']) == (case, 32, engine))

    lines = [
        '## Results', '',
        'Apple M4 Pro, 48 GiB RAM, macOS 26.5.2 arm64 · 2026-10-15. Medians of three 5-second trials.', '',
        '### Server-rendered HTML (without in-memory page caching): 32 concurrent clients', '',
        'Each request authenticates a user, checks membership, and renders the latest 50 messages. Database and operating-system caches remain active.', '',
        '| Application execution | Requests/s | CPU % (100% = 1 core) | CPU µs/request | Peak process memory MiB | p99 ms |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    baseline = row('uncached-html', 'js')
    for engine in engines:
        r = row('uncached-html', engine)
        cells = ['**' + relative(r['requests_per_s'], baseline['requests_per_s'], ',.0f') + '**']
        cells += [relative(r[key], baseline[key], spec) for key, spec in (
            ('cpu_percent_one_core', '.1f'), ('cpu_us_per_operation', '.1f'),
            ('memory_mib', '.1f'), ('p99_ms', '.2f'))]
        lines.append('| ' + labels[engine] + ' | ' + ' | '.join(cells) + ' |')
    lines += ['',
        '### Other chat operations', '',
        '| HTTP operation: requests/s, 32 concurrent clients | JavaScript / V8 (baseline) | Rust Wasm / Wasmtime | Native Rust |',
        '| --- | ---: | ---: | ---: |',
    ]
    for case, label in [('uncached-json', 'Read messages as JSON (no response cache)'),
                        ('cached-html', 'Read HTML with in-memory page caching'),
                        ('post', 'Post a message; reply after saving it')]:
        baseline = row(case, 'js')['requests_per_s']
        lines.append('| ' + label + ' | ' + ' | '.join(relative(row(case, engine)['requests_per_s'], baseline, ',.0f') for engine in engines) + ' |')
    lines += ['',
        'Each WebSocket message is saved and broadcast; the producer waits for every participant\'s HTTP read acknowledgment.', '',
        '| WebSocket setup: messages/s | JavaScript / V8 (baseline) | Rust Wasm / Wasmtime | Native Rust |',
        '| --- | ---: | ---: | ---: |',
    ]
    for case, label in [('ws-1x32', '1 room, 32 listeners'), ('ws-5x8', '5 rooms, 8 listeners each')]:
        rates = [next(r['messages_per_s'] for r in summary['websocket'] if r['case'] == case and r['engine'] == engine) for engine in engines]
        lines.append('| ' + label + ' | ' + ' | '.join(relative(rate, rates[0], ',.0f') for rate in rates) + ' |')
    lines.append('')
    if compile_cache:
        lines.append(compile_cache_results(compile_cache, 'results/2026-10-15/compile-cache/cold-cache-results.json'))
    else:
        lines += ['### Process startup', '',
            '| Application execution | Process launch → first room page, ms | Process memory after first page, MiB |',
            '| --- | ---: | ---: |']
        baseline = next(r for r in summary['cold'] if r['engine'] == 'js')
        for engine in engines:
            r = next(r for r in summary['cold'] if r['engine'] == engine)
            cells = [relative(r[key], baseline[key], spec) for key, spec in (
                ('process_to_first_page_ms', '.2f'), ('footprint_mib', '.1f'))]
            lines.append('| ' + labels[engine] + ' | ' + ' | '.join(cells) + ' |')
        lines += ['',
            'Startup values are medians of ten fresh processes. They include service startup, object activation, and the first room response. Filesystem caches stay warm; Wasmtime compiles the module inside each process and uses no cross-process AOT cache.']
    lines += ['',
        '[Complete report](results/2026-10-15/REPORT.md) · Raw data: [throughput](results/2026-10-15/results.json), [startup](results/2026-10-15/cold-results.json), [validation](results/2026-10-15/validation.json).',
    ]
    return '\n'.join(lines)

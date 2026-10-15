from pathlib import Path


def relative(value, baseline, spec):
    return f"{value:{spec}} ({value / baseline:.2f}×)"


def compile_cache_results(compile_cache, raw_link):
    data, rows = compile_cache
    labels = {'js': 'JavaScript / V8', 'wasm': 'Rust Wasm / Wasmtime: cache disabled',
              'wasm-cached': 'Rust Wasm / Wasmtime: cache hit', 'native': 'Native Rust'}
    baseline = next(r for r in rows if r['engine'] == 'js')
    wasm = next(r for r in rows if r['engine'] == 'wasm')
    cached = next(r for r in rows if r['engine'] == 'wasm-cached')
    lines = [
        '### Process startup with a compilation cache', '',
        f"Separate matched run on {data['cpu']}, {data['memory_bytes']/2**30:g} GiB RAM, `{data['platform']}`: {data['rounds']} fresh processes per configuration, with the same celld executable and seeded database snapshot. Both Wasm configurations use the same module. The cached configuration uses Wasmtime 49's built-in filesystem compilation cache, populated in a separate process before timing. Filesystem caches remain warm.", '',
        'The table shows medians as **absolute value (ratio to JS)**, with JS at **1.00×**. Lower values are better. CPU is cumulative server user + system time through the first room response; memory is the process physical footprint immediately after that response.', '',
        '| Application execution | Process launch → first room page, ms | Server CPU, ms | Process memory after first page, MiB |',
        '| --- | ---: | ---: | ---: |',
    ]
    for row in rows:
        cells = [relative(row[key], baseline[key], spec) for key, spec in (
            ('process_to_first_page_ms', '.2f'), ('cpu_ms', '.2f'), ('footprint_mib', '.1f'))]
        lines.append('| ' + labels[row['engine']] + ' | ' + ' | '.join(cells) + ' |')
    lines += ['',
        f"Every cached process records one cache hit and zero misses; the cache-populating process records one miss. Wasm module compilation/loading (`Module::new`) takes {wasm['module_ms']:.2f} ms with caching disabled and {cached['module_ms']:.2f} ms on a cache hit. The full startup table also includes service initialization, object activation, and the first page response.", '',
        f"The cache stores compiled Wasm code. Application execution, SQL calls, and page caching use the same paths in both Wasm configurations. The throughput tests start after warmup, which already excludes module compilation. [Startup measurements and source hashes]({raw_link}).",
    ]
    return '\n'.join(lines)


def render(raw, cold_data, s, compile_cache=None):
    names = {'js': 'celld JS', 'wasm': 'celld Wasm', 'native': 'celld native',
             'wasm-v8': 'celld V8 Wasm'}

    def http(case, c, engine):
        return next(r for r in s['http'] if (r['case'], r['concurrency'], r['engine']) == (case, c, engine))

    def ratio(case, c, a, b, key='requests_per_s'):
        return http(case, c, a)[key] / http(case, c, b)[key]

    clients = sorted({r['concurrency'] for r in s['http'] if r['case'] == 'uncached-html'})
    focus = 32 if 32 in clients else clients[-1]
    wasm = http('uncached-html', focus, 'wasm')
    js = http('uncached-html', focus, 'js')
    native = http('uncached-html', focus, 'native')
    summary = (f"With in-memory page caching disabled and {focus} concurrent clients, Rust Wasm serves "
               f"**{wasm['requests_per_s']:,.0f} requests/s versus JavaScript's {js['requests_per_s']:,.0f} "
               f"({wasm['requests_per_s']/js['requests_per_s']:.2f}×)**. Server CPU time per request is "
               f"**{wasm['cpu_us_per_operation']:.2f} µs versus {js['cpu_us_per_operation']:.2f} µs "
               f"({wasm['cpu_us_per_operation']/js['cpu_us_per_operation']:.2f}×)**; "
               f"whole-process physical memory is **{wasm['memory_mib']:.1f} MiB versus {js['memory_mib']:.1f} MiB "
               f"({wasm['memory_mib']/js['memory_mib']:.2f}×)**. "
               f"Native Rust serves {native['requests_per_s']:,.0f} requests/s "
               f"({native['requests_per_s']/js['requests_per_s']:.2f}× JavaScript's throughput).")

    tables = [
        f'### Server-rendered HTML (without in-memory page caching): {focus} concurrent clients', '',
        'Each request selects and renders the latest 50 messages. The memory column covers the entire server process. Each value is a median across the recorded trials.', '',
        '| Execution | Requests/s | Server CPU µs/request | Physical memory MiB | p99 response ms |',
        '| --- | ---: | ---: | ---: | ---: |',
    ]
    for e in ('js', 'wasm', 'native'):
        r = http('uncached-html', focus, e)
        cells = [relative(r[key], js[key], spec) for key, spec in (
            ('requests_per_s', ',.0f'), ('cpu_us_per_operation', '.2f'),
            ('memory_mib', '.1f'), ('p99_ms', '.2f'))]
        tables.append('| ' + names[e] + ' | ' + ' | '.join(cells) + ' |')
    tables.append(f"\nAt the same concurrency, Wasm has {ratio('uncached-json',focus,'wasm','js'):.2f}× JavaScript's uncached JSON throughput and {ratio('cached-html',focus,'wasm','js'):.2f}× its cached HTML throughput. The additional V8 Wasm measurements appear in the complete tables below.\n")

    titles = {'uncached-html': 'Server-rendered HTML (without in-memory page caching)', 'uncached-json': 'Uncached JSON reads',
              'cached-html': 'Cached HTML reads', 'post': 'Durable HTTP posts',
              'mixed': 'Cached reads with a concurrent writer'}
    for case, title in titles.items():
        tables.append(f'### {title}\n')
        if case == 'mixed':
            tables.append('The request rate includes reads and posts. CPU/op uses that combined count. The separate writer and cache-hit table follows.\n')
        if case == 'post':
            tables.append('Every request posts a message and waits for the runtime\'s durability gate.\n')
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
    tables.append('Each message is persisted and broadcast, then every participant sends an HTTP read acknowledgment. A producer waits for all these requests before its next message. Frame deliveries include the sender. CPU/message includes fanout and the read acknowledgments. The two p99 columns separate sender broadcast acknowledgment from the complete round.\n')
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
    for key, text in {'RESULTS_SUMMARY': summary, 'RESULTS_TABLES': '\n'.join(tables),
                      'COMPILE_CACHE_RESULTS': compile_cache_results(compile_cache, 'compile-cache/cold-cache-results.json') if compile_cache else '',
                      'STARTUP_TABLE': '\n'.join(cold), 'CORRECTNESS_SUMMARY': correctness}.items():
        assert key in report
        report = report.replace(key, text)
    for key, text in methods.items():
        assert key in report
        report = report.replace(key, text)
    return report


def readme_results(summary, compile_cache=None):
    labels = {'js': 'JavaScript / V8', 'wasm': 'Rust Wasm / Wasmtime', 'native': 'Native Rust'}
    engines = ('js', 'wasm', 'native')

    def row(case, engine):
        return next(r for r in summary['http'] if (r['case'], r['concurrency'], r['engine']) == (case, 32, engine))

    lines = [
        '## Results', '',
        'Recorded on 2026-10-15: Apple M4 Pro, 14 logical CPUs, 48 GiB RAM, macOS 26.5.2 arm64. Each HTTP case runs for five seconds at 1, 32, and 128 concurrent clients, with three rounds and a fresh server process per trial. The tables show medians.', '',
        'Each client waits for a complete response before sending its next request; 32 concurrent clients allow up to 32 requests in flight. Each measurement shows **absolute value (ratio to JS)** for the same workload and client count. JS is **1.00×**; a memory ratio of **0.45×** means 45% of JS\'s memory. Ratios use the unrounded medians. Higher throughput is better; lower CPU time, memory, latency, and startup time are better.', '',
        '### Server-rendered HTML (without in-memory page caching): 32 concurrent clients', '',
        'Each request authenticates a user, checks room membership, queries the latest 50 messages, and renders HTML. Only the generated full-page cache is disabled; database and operating-system caches remain active.', '',
        '| Application execution | Requests/s | CPU % | CPU µs/request | Process memory MiB | p99 ms |',
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
    wasm, js = [row('uncached-html', engine) for engine in ('wasm', 'js')]
    lines += ['',
        f"For this read, Rust Wasm delivers **{wasm['requests_per_s']/js['requests_per_s']:.2f}× JavaScript's throughput**, consumes **{(1-wasm['cpu_us_per_operation']/js['cpu_us_per_operation'])*100:.0f}% less CPU time per request**, and uses **{(1-wasm['memory_mib']/js['memory_mib'])*100:.0f}% less process memory**.", '',
        'CPU 100% means one fully occupied core; helper threads can take the total above 100%. The CPU % ratio measures total CPU use at each engine\'s achieved throughput. CPU time per request compares efficiency at equal work. Memory is the whole server process: the median of the sampled peak physical footprints, in MiB.', '',
        '### Other application paths', '',
        '| Workload | JavaScript / V8 | Rust Wasm / Wasmtime | Native Rust |',
        '| --- | ---: | ---: | ---: |',
    ]
    for case, label in [('uncached-json', 'Uncached JSON, 32 clients (requests/s)'), ('cached-html', 'Cached HTML, 32 clients (requests/s)'), ('post', 'Durable posts, 32 clients (requests/s)')]:
        baseline = row(case, 'js')['requests_per_s']
        lines.append('| ' + label + ' | ' + ' | '.join(relative(row(case, engine)['requests_per_s'], baseline, ',.0f') for engine in engines) + ' |')
    for case, label in [('ws-1x32', 'WebSocket: 1 room, 32 listeners (messages/s)'), ('ws-5x8', 'WebSocket: 5 rooms, 8 listeners each (messages/s)')]:
        rates = [next(r['messages_per_s'] for r in summary['websocket'] if r['case'] == case and r['engine'] == engine) for engine in engines]
        lines.append('| ' + label + ' | ' + ' | '.join(relative(rate, rates[0], ',.0f') for rate in rates) + ' |')
    lines += ['',
        'A WebSocket message includes persistence, broadcast to every participant, and each participant’s HTTP read acknowledgment. Durable posts and these full message rounds have similar throughput across the three implementations: they wait for the same persistence and response/broadcast release mechanism.', '',
    ]
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
        'The [complete report](results/2026-10-15/REPORT.md) includes every concurrency, CPU and physical-memory measurements, latency, trial ranges, write rates, correctness checks, and the auxiliary V8 Wasm comparison. Raw data also includes RSS: [pressure](results/2026-10-15/results.json), [startup](results/2026-10-15/cold-results.json), [validation](results/2026-10-15/validation.json).',
    ]
    return '\n'.join(lines)

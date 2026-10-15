from pathlib import Path


def render(raw, cold_data, s):
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
    summary = (f"At {focus} concurrent uncached HTML reads, Rust Wasm serves "
               f"**{wasm['requests_per_s']:,.0f} requests/s versus JavaScript's {js['requests_per_s']:,.0f} "
               f"({wasm['requests_per_s']/js['requests_per_s']:.2f}×)**. Server CPU time per request is "
               f"**{wasm['cpu_us_per_operation']:.2f} µs versus {js['cpu_us_per_operation']:.2f} µs**; "
               f"whole-process physical memory is **{wasm['memory_mib']:.1f} MiB versus {js['memory_mib']:.1f} MiB**. "
               f"Native Rust serves {native['requests_per_s']:,.0f} requests/s; "
               f"Wasm reaches {wasm['requests_per_s']/native['requests_per_s']*100:.0f}% of that throughput.")

    tables = [
        f'### A comparable read: {focus} concurrent clients, latest 50 messages as HTML', '',
        'Lower CPU time, memory, and latency are better; higher requests/s is better. The memory column covers the entire server process. Each value is a median across the recorded trials.', '',
        '| Execution | Requests/s | Server CPU µs/request | Physical memory MiB | p99 response ms |',
        '| --- | ---: | ---: | ---: | ---: |',
    ]
    for e in ('js', 'wasm', 'native'):
        r = http('uncached-html', focus, e)
        tables.append(f"| {names[e]} | {r['requests_per_s']:,.0f} | {r['cpu_us_per_operation']:.2f} | {r['memory_mib']:.1f} | {r['p99_ms']:.2f} |")
    tables.append(f"\nAt the same concurrency, Wasm has {ratio('uncached-json',focus,'wasm','js'):.2f}× JavaScript's uncached JSON throughput and {ratio('cached-html',focus,'wasm','js'):.2f}× its cached HTML throughput. The additional V8 Wasm measurements appear in the complete tables below.\n")

    titles = {'uncached-html': 'Uncached HTML reads', 'uncached-json': 'Uncached JSON reads',
              'cached-html': 'Cached HTML reads', 'post': 'Durable HTTP posts',
              'mixed': 'Cached reads with a concurrent writer'}
    for case, title in titles.items():
        tables.append(f'### {title}\n')
        if case == 'mixed':
            tables.append('The request rate includes reads and posts. CPU/op uses that combined count. The separate writer and cache-hit table follows.\n')
        if case == 'post':
            tables.append('Every request posts a message and waits for the runtime\'s durability gate.\n')
        tables.append('| Clients | Execution | Requests/s (trial range) | CPU µs/op | CPU % | Physical MiB | p99 ms |')
        tables.append('| ---: | --- | ---: | ---: | ---: | ---: | ---: |')
        for r in [r for r in s['http'] if r['case'] == case]:
            tables.append(f"| {r['concurrency']} | {names[r['engine']]} | {r['requests_per_s']:,.0f} ({r['throughput_min']:,.0f}–{r['throughput_max']:,.0f}) | {r['cpu_us_per_operation']:.2f} | {r['cpu_percent_one_core']:.1f} | {r['memory_mib']:.1f} | {r['p99_ms']:.2f} |")
        if case == 'mixed':
            tables.append('\n| Read clients | Execution | Reads/s | Posts/s | Cache hits % |')
            tables.append('| ---: | --- | ---: | ---: | ---: |')
            for r in [r for r in s['http'] if r['case'] == case]:
                tables.append(f"| {r['concurrency']} | {names[r['engine']]} | {r['reads_per_s']:,.0f} | {r['writes_per_s']:.1f} | {r['cache_hit_percent']:.1f} |")
        tables.append('')

    tables.append('### Live WebSocket messages\n')
    tables.append('Each message is persisted and broadcast, then every participant sends an HTTP read acknowledgment. A producer waits for all these requests before its next message. Frame deliveries include the sender. CPU/message includes fanout and the read acknowledgments. The two p99 columns separate sender broadcast acknowledgment from the complete round.\n')
    tables.append('| Rooms × listeners | Execution | Messages/s (trial range) | Frame deliveries/s | HTTP read acks/s | CPU µs/message | CPU % | Physical MiB | Sender p99 ms | Complete round p99 ms |')
    tables.append('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |')
    for r in s['websocket']:
        tables.append(f"| {r['case'][3:]} | {names[r['engine']]} | {r['messages_per_s']:,.0f} ({r['throughput_min']:,.0f}–{r['throughput_max']:,.0f}) | {r['deliveries_per_s']:,.0f} | {r['read_acknowledgments_per_s']:,.0f} | {r['cpu_us_per_operation']:.2f} | {r['cpu_percent_one_core']:.1f} | {r['memory_mib']:.1f} | {r['sender_p99_ms']:.2f} | {r['p99_ms']:.2f} |")

    cold = ['| Execution | Process → root ms | First room after root ms | Process → first page ms (range) | Physical MiB after first page |',
            '| --- | ---: | ---: | ---: | ---: |']
    for r in s['cold']:
        cold.append(f"| {names[r['engine']]} | {r['process_to_root_ms']:.2f} | {r['first_room_ms']:.2f} | {r['process_to_first_page_ms']:.2f} ({r['process_to_first_page_ms_min']:.2f}–{r['process_to_first_page_ms_max']:.2f}) | {r['footprint_mib']:.1f} |")

    checks = s['checks']
    correctness = f"The reported measurements contain **{checks['http_trials']} HTTP trials, {checks['websocket_trials']} WebSocket trials, and {checks['total_cold_trials']} startup trials**, plus **{checks['dedicated_http_checks']:,} dedicated full-response checks**. The HTTP pressure clients completed {checks['http_requests']:,} operations, including {checks['http_posts']:,} posts. The WebSocket sessions added {checks['websocket_messages']:,} posts, {checks['websocket_deliveries_including_sender']:,} verified frame deliveries, and {checks['websocket_read_acknowledgments']:,} verified HTTP read acknowledgments. All trials passed response and ordering checks; all {checks['database_audits']} post-trial audits passed across all 50 room databases. Source hashes agree across validation, pressure, and startup. Captured source files match their recorded SHA-256 hashes; `recorded_only` lists the executable and original-harness digests retained as provenance.\n\nThe pressure run started at {raw['started_at']} and completed at {raw['completed_at']}. The pressure JSON records the before/after thermal checks. The largest recorded load-client CPU usage was {checks['max_client_cpu_percent_one_core']:.1f}% of one core; clients ran in separate processes on the same machine."

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
                      'STARTUP_TABLE': '\n'.join(cold), 'CORRECTNESS_SUMMARY': correctness}.items():
        assert key in report
        report = report.replace(key, text)
    for key, text in methods.items():
        assert key in report
        report = report.replace(key, text)
    return report


def readme_results(summary):
    labels = {'js': 'JavaScript / V8', 'wasm': 'Rust Wasm / Wasmtime', 'native': 'Native Rust'}
    engines = ('js', 'wasm', 'native')

    def row(case, engine):
        return next(r for r in summary['http'] if (r['case'], r['concurrency'], r['engine']) == (case, 32, engine))

    lines = [
        '## Results', '',
        'Recorded on 2026-10-15: Apple M4 Pro, 14 logical CPUs, 48 GiB RAM, macOS 26.5.2 arm64. Each HTTP case runs for five seconds at 1, 32, and 128 concurrent clients, with three rounds and a fresh server process per trial. The tables show medians.', '',
        '### Uncached HTML: 32 concurrent clients', '',
        'Each request authenticates a user, checks room membership, queries the latest 50 messages, and renders HTML. Higher requests/s is better; lower CPU time, memory, and latency are better.', '',
        '| Application execution | Requests/s | CPU % | CPU µs/request | Process memory MiB | p99 ms |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    for engine in engines:
        r = row('uncached-html', engine)
        lines.append(f"| {labels[engine]} | **{r['requests_per_s']:,.0f}** | {r['cpu_percent_one_core']:.1f} | {r['cpu_us_per_operation']:.1f} | {r['memory_mib']:.1f} | {r['p99_ms']:.2f} |")
    wasm, js, native = [row('uncached-html', engine) for engine in ('wasm', 'js', 'native')]
    lines += ['',
        f"For this read, Rust Wasm delivers **{wasm['requests_per_s']/js['requests_per_s']:.2f}× JavaScript's throughput**, consumes **{(1-wasm['cpu_us_per_operation']/js['cpu_us_per_operation'])*100:.0f}% less CPU time per request**, and uses **{(1-wasm['memory_mib']/js['memory_mib'])*100:.0f}% less process memory**. Its throughput is **{wasm['requests_per_s']/native['requests_per_s']*100:.0f}% of native Rust's**.", '',
        'CPU 100% means one fully occupied core; helper threads can take the total above 100%. CPU time per request compares efficiency at equal work. Memory is the whole server process: the median of the sampled peak physical footprints, in MiB.', '',
        '### Other application paths', '',
        '| Workload | JavaScript / V8 | Rust Wasm / Wasmtime | Native Rust |',
        '| --- | ---: | ---: | ---: |',
    ]
    for case, label in [('uncached-json', 'Uncached JSON, 32 clients (requests/s)'), ('cached-html', 'Cached HTML, 32 clients (requests/s)'), ('post', 'Durable posts, 32 clients (requests/s)')]:
        lines.append('| ' + label + ' | ' + ' | '.join(f"{row(case, engine)['requests_per_s']:,.0f}" for engine in engines) + ' |')
    for case, label in [('ws-1x32', 'WebSocket: 1 room, 32 listeners (messages/s)'), ('ws-5x8', 'WebSocket: 5 rooms, 8 listeners each (messages/s)')]:
        rates = [next(r['messages_per_s'] for r in summary['websocket'] if r['case'] == case and r['engine'] == engine) for engine in engines]
        lines.append('| ' + label + ' | ' + ' | '.join(f'{rate:,.0f}' for rate in rates) + ' |')
    lines += ['',
        'A WebSocket message includes persistence, broadcast to every participant, and each participant’s HTTP read acknowledgment. Durable posts and these full message rounds have similar throughput across the three implementations: they wait for the same persistence and response/broadcast release mechanism.', '',
        '### Process startup', '',
        '| Application execution | Process launch → first room page, ms | Process memory after first page, MiB |',
        '| --- | ---: | ---: |',
    ]
    for engine in engines:
        r = next(r for r in summary['cold'] if r['engine'] == engine)
        lines.append(f"| {labels[engine]} | {r['process_to_first_page_ms']:.2f} | {r['footprint_mib']:.1f} |")
    lines += ['',
        'Startup values are medians of ten fresh processes. They include service startup, object activation, and the first room response. Filesystem caches stay warm; Wasmtime compiles the module inside each process and uses no cross-process AOT cache.', '',
        'The [complete report](results/2026-10-15/REPORT.md) includes every concurrency, CPU and physical-memory measurements, latency, trial ranges, write rates, correctness checks, and the auxiliary V8 Wasm comparison. Raw data also includes RSS: [pressure](results/2026-10-15/results.json), [startup](results/2026-10-15/cold-results.json), [validation](results/2026-10-15/validation.json).',
    ]
    return '\n'.join(lines)

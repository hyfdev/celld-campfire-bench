use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

#[derive(Deserialize, Clone)]
struct Expected {
    room: u32,
    user: u32,
    json: bool,
    version: u64,
    length: usize,
    crc32: u32,
}

#[derive(Deserialize)]
struct Manifest {
    post_text: String,
    post_json: String,
    reads: Vec<Expected>,
}

struct Oracle {
    post_text: String,
    post_json: String,
    reads: HashMap<(u32, u32, bool, u64), Expected>,
    committed: Vec<AtomicU64>,
    initial_version: u64,
}

#[derive(Serialize)]
struct Sample {
    room: u32,
    version: u64,
    write: bool,
    hit: bool,
    latency_ns: u64,
}

async fn request(
    client: &reqwest::Client,
    base: &str,
    case: &str,
    oracle: &Oracle,
    room: u32,
    user: u32,
    json: bool,
    write: bool,
) -> Result<Sample> {
    let minimum = oracle.committed[room as usize - 1].load(Ordering::Acquire);
    let path = format!(
        "{base}/rooms/{room}{}",
        if json || write { "/messages" } else { "" }
    );
    let started = Instant::now();
    let builder = if write {
        client
            .post(path)
            .body(oracle.post_json.clone())
            .header("content-type", "application/json")
    } else {
        client.get(path)
    };
    let response = builder
        .header("authorization", format!("Bearer token-{user}"))
        .header(
            "x-chat-cache",
            if case.starts_with("cached") || case == "mixed" {
                "1"
            } else {
                "0"
            },
        )
        .send()
        .await?;
    let status = response.status().as_u16();
    if status != if write { 201 } else { 200 } {
        let body = response.text().await?;
        anyhow::bail!("HTTP {status}, room {room}, write {write}: {body}");
    }
    let header = |key: &str| -> Result<&str> {
        response
            .headers()
            .get(key)
            .with_context(|| format!("missing {key}, HTTP {status}, room {room}"))?
            .to_str()
            .map_err(Into::into)
    };
    let version = header("x-chat-version")?.parse::<u64>()?;
    let hit = header("x-chat-cache-hit")? == "1";
    let sql_calls = header("x-chat-sql-calls")?.parse::<u32>()?;
    anyhow::ensure!(
        header("content-type")?
            == if json || write {
                "application/json"
            } else {
                "text/html; charset=utf-8"
            },
        "wrong content type"
    );
    let bytes = response.bytes().await?;
    let latency_ns = started.elapsed().as_nanos() as u64;
    anyhow::ensure!(
        version >= minimum,
        "response version {version} predates acknowledged write {minimum}"
    );
    if write {
        anyhow::ensure!(
            status == 201 && !hit && sql_calls == 4 && version > oracle.initial_version,
            "invalid write metadata"
        );
        let value: serde_json::Value = serde_json::from_slice(&bytes)?;
        let expected = serde_json::json!({
            "id": 1000 + version - 1, "room_id": room, "user_id": 1,
            "body": oracle.post_text, "created_at": 1700000000u64 + version - 1, "version": version,
        });
        anyhow::ensure!(value == expected, "write response differs: {value}");
        oracle.committed[room as usize - 1].fetch_max(version, Ordering::Release);
    } else {
        anyhow::ensure!(
            status == 200 && sql_calls == if hit { 3 } else { 4 },
            "invalid read metadata"
        );
        if case.starts_with("cached") {
            anyhow::ensure!(hit, "warm page cache missed");
        }
        if case.starts_with("uncached") {
            anyhow::ensure!(!hit, "disabled page cache hit");
        }
        let expected = oracle
            .reads
            .get(&(room, user, json, version))
            .context("version exceeds independent oracle range")?;
        anyhow::ensure!(
            bytes.len() == expected.length && crc32fast::hash(&bytes) == expected.crc32,
            "page differs: room {room}, user {user}, version {version}"
        );
    }
    Ok(Sample {
        room,
        version,
        write,
        hit,
        latency_ns,
    })
}

fn percentile(samples: &[Sample], get: impl Fn(&Sample) -> u64, percent: usize) -> f64 {
    let mut values: Vec<u64> = samples.iter().map(get).collect();
    if values.is_empty() {
        return 0.;
    }
    values.sort_unstable();
    values[(values.len() * percent / 100).min(values.len() - 1)] as f64 / 1e6
}

async fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    anyhow::ensure!(
        args.len() == 8,
        "usage: workers-chat-load BASE_URL CASE CONCURRENCY SECONDS INITIAL_VERSION ORACLE OUTPUT"
    );
    let concurrency: usize = args[3].parse()?;
    let seconds: f64 = args[4].parse()?;
    let initial_version: u64 = args[5].parse()?;
    let manifest: Manifest = serde_json::from_slice(&std::fs::read(&args[6])?)?;
    let oracle = Arc::new(Oracle {
        post_text: manifest.post_text,
        post_json: manifest.post_json,
        reads: manifest
            .reads
            .into_iter()
            .map(|r| ((r.room, r.user, r.json, r.version), r))
            .collect(),
        committed: (0..50).map(|_| AtomicU64::new(initial_version)).collect(),
        initial_version,
    });
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .pool_max_idle_per_host(256)
        .tcp_nodelay(true)
        .build()?;
    let start = Instant::now() + Duration::from_millis(50);
    let end = start + Duration::from_secs_f64(seconds);
    let mut tasks = tokio::task::JoinSet::new();
    for lane in 0..concurrency {
        let (client, oracle, base, case) = (
            client.clone(),
            oracle.clone(),
            args[1].clone(),
            args[2].clone(),
        );
        tasks.spawn(async move {
            tokio::time::sleep_until(tokio::time::Instant::from_std(start)).await;
            let mut index = 0usize;
            let mut samples = Vec::new();
            let mut errors = Vec::new();
            while Instant::now() < end {
                let room = ((index * 17 + lane * 13) % 50 + 1) as u32;
                let write = case == "post";
                let user = if write {
                    1
                } else {
                    ((index + lane) % 4 + 1) as u32
                };
                let json = case.ends_with("json") || (case == "mixed" && (index + lane) % 5 == 0);
                match request(&client, &base, &case, &oracle, room, user, json, write).await {
                    Ok(sample) => samples.push(sample),
                    Err(error) => {
                        errors.push(format!("{error:#}"));
                        break;
                    }
                }
                index += 1;
            }
            (samples, errors)
        });
    }
    if args[2] == "mixed" {
        let (client, oracle, base) = (client.clone(), oracle.clone(), args[1].clone());
        tasks.spawn(async move {
            tokio::time::sleep_until(tokio::time::Instant::from_std(start)).await;
            let mut timer = tokio::time::interval(Duration::from_millis(10));
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let mut samples = Vec::new();
            let mut errors = Vec::new();
            let mut index = 0;
            loop {
                timer.tick().await;
                if Instant::now() >= end {
                    break;
                }
                match request(
                    &client,
                    &base,
                    "mixed",
                    &oracle,
                    index % 50 + 1,
                    1,
                    true,
                    true,
                )
                .await
                {
                    Ok(sample) => samples.push(sample),
                    Err(error) => {
                        errors.push(format!("{error:#}"));
                        break;
                    }
                }
                index += 1;
            }
            (samples, errors)
        });
    }
    let mut samples = Vec::new();
    let mut errors = Vec::new();
    while let Some(result) = tasks.join_next().await {
        let (mut rows, mut failures) = result?;
        samples.append(&mut rows);
        errors.append(&mut failures);
    }
    let elapsed_s = start.elapsed().as_secs_f64();
    let mut write_counts = vec![0u64; 50];
    let mut versions: Vec<HashSet<u64>> = (0..50).map(|_| HashSet::new()).collect();
    for row in samples.iter().filter(|r| r.write) {
        let room = row.room as usize - 1;
        write_counts[room] += 1;
        anyhow::ensure!(
            versions[room].insert(row.version),
            "duplicate write acknowledgment"
        );
    }
    for room in 0..50 {
        anyhow::ensure!(
            versions[room]
                .iter()
                .all(|v| *v > initial_version && *v <= initial_version + write_counts[room]),
            "write acknowledgment sequence has a gap"
        );
    }
    let writes: u64 = write_counts.iter().sum();
    let reads = samples.len() as u64 - writes;
    let hits = samples.iter().filter(|r| !r.write && r.hit).count();
    let result = serde_json::json!({
        "case": args[2], "concurrency": concurrency, "writer_concurrency": if args[2] == "mixed" { 1 } else { 0 },
        "target_seconds": seconds, "elapsed_s": elapsed_s, "successful": samples.len(), "reads": reads, "writes": writes,
        "errors": errors, "requests_per_s": samples.len() as f64 / elapsed_s, "reads_per_s": reads as f64 / elapsed_s,
        "writes_per_s": writes as f64 / elapsed_s, "cache_hits": hits, "write_counts": write_counts,
        "p50_ms": percentile(&samples, |s| s.latency_ns, 50), "p99_ms": percentile(&samples, |s| s.latency_ns, 99),
        "initial_version": initial_version,
    });
    std::fs::write(&args[7], serde_json::to_vec_pretty(&result)?)?;
    println!(
        "{} c={} {:.0} requests/s, p99 {:.3} ms, errors {}",
        args[2],
        concurrency,
        result["requests_per_s"].as_f64().unwrap(),
        result["p99_ms"].as_f64().unwrap(),
        result["errors"].as_array().unwrap().len()
    );
    anyhow::ensure!(errors.is_empty(), "invalid response: {errors:?}");
    Ok(())
}

fn main() -> Result<()> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()?
        .block_on(run())
}

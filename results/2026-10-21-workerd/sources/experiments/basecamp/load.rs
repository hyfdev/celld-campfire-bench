use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Deserialize)]
struct Page {
    room: u32,
    user: usize,
    length: usize,
    crc32: u32,
}

#[derive(Deserialize)]
struct Manifest {
    base: String,
    cookies: Vec<String>,
    pages: Vec<Page>,
    post_html: String,
}

#[derive(Serialize)]
struct Receipt {
    id: u64,
    room: u32,
    client_message_id: String,
}

fn ids(html: &str) -> Result<Vec<u64>> {
    html.split("data-message-id=\"")
        .skip(1)
        .map(|s| s.split('"').next().unwrap().parse().map_err(Into::into))
        .collect()
}

async fn request(
    client: &reqwest::Client,
    manifest: &Manifest,
    case: &str,
    lane: usize,
    index: usize,
) -> Result<(u64, usize, Option<Receipt>)> {
    let room = (index * 17 + lane * 13) % 50 + 1;
    let post = case == "post";
    let user = if post { 1 } else { (index + lane) % 4 + 1 };
    let path = if case == "room-html" {
        format!("/rooms/{room}")
    } else {
        format!("/rooms/{room}/messages")
    };
    let client_message_id = format!("bench-{lane}-{index}");
    let builder = if post {
        client.post(format!("{}{path}", manifest.base)).form(&[
            ("message[body]", manifest.post_html.as_str()),
            ("message[client_message_id]", client_message_id.as_str()),
        ])
    } else {
        client.get(format!("{}{path}", manifest.base))
    };
    let started = Instant::now();
    let response = builder
        .header("cookie", &manifest.cookies[user - 1])
        .header("accept-encoding", "identity")
        .header("sec-fetch-site", "same-origin")
        .header(
            "accept",
            if post {
                "text/vnd.turbo-stream.html"
            } else {
                "text/html"
            },
        )
        .send()
        .await?;
    let status = response.status().as_u16();
    let content_type = response
        .headers()
        .get("content-type")
        .context("missing content type")?
        .to_str()?
        .to_owned();
    anyhow::ensure!(
        response.headers().get("content-encoding").is_none(),
        "unexpected compression"
    );
    let body = response.bytes().await?;
    let elapsed = started.elapsed().as_nanos() as u64;
    anyhow::ensure!(
        status == 200,
        "HTTP {status}: {}",
        String::from_utf8_lossy(&body[..body.len().min(200)])
    );
    let receipt = if post {
        anyhow::ensure!(
            content_type.starts_with("text/vnd.turbo-stream.html"),
            "wrong POST content type"
        );
        let html = std::str::from_utf8(&body)?;
        let actual_ids = ids(html)?;
        anyhow::ensure!(actual_ids.len() == 1, "wrong POST message count");
        anyhow::ensure!(
            html.starts_with("<turbo-stream")
                && html.trim_end().ends_with("</turbo-stream>")
                && html.contains("action=\"append\"")
                && html.contains(&format!("target=\"messages_rooms_open_{room}\""))
                && html.contains(&format!("id=\"message_{client_message_id}\""))
                && html.contains("New message")
                && html.contains("你好"),
            "incomplete POST representation"
        );
        Some(Receipt {
            id: actual_ids[0],
            room: room as u32,
            client_message_id,
        })
    } else {
        anyhow::ensure!(
            content_type.starts_with("text/html"),
            "wrong read content type"
        );
        let page = &manifest.pages[(room - 1) * 4 + user - 1];
        anyhow::ensure!(
            page.room == room as u32 && page.user == user,
            "oracle order differs"
        );
        anyhow::ensure!(
            body.len() == page.length && crc32fast::hash(&body) == page.crc32,
            "read differs from validated page"
        );
        None
    };
    Ok((elapsed, body.len(), receipt))
}

fn percentile(values: &[u64], percent: usize) -> f64 {
    values[(values.len() * percent / 100).min(values.len() - 1)] as f64 / 1e6
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    anyhow::ensure!(
        args.len() == 6,
        "usage: campfire-original-load MANIFEST CASE CONCURRENCY SECONDS OUTPUT"
    );
    let manifest: Manifest = serde_json::from_slice(&std::fs::read(&args[1])?)?;
    let manifest = Arc::new(manifest);
    let concurrency: usize = args[3].parse()?;
    let seconds: f64 = args[4].parse()?;
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(30))
        .pool_max_idle_per_host(256)
        .tcp_nodelay(true)
        .build()?;
    let start = Instant::now() + Duration::from_millis(50);
    let end = start + Duration::from_secs_f64(seconds);
    let mut tasks = tokio::task::JoinSet::new();
    for lane in 0..concurrency {
        let (client, manifest, case) = (client.clone(), manifest.clone(), args[2].clone());
        tasks.spawn(async move {
            tokio::time::sleep_until(tokio::time::Instant::from_std(start)).await;
            let mut times = Vec::new();
            let mut receipts = Vec::new();
            let mut errors = Vec::new();
            let mut bytes = 0usize;
            while Instant::now() < end {
                match request(&client, &manifest, &case, lane, times.len()).await {
                    Ok((elapsed, length, receipt)) => {
                        times.push(elapsed);
                        bytes += length;
                        receipts.extend(receipt);
                    }
                    Err(error) => {
                        errors.push(format!("{error:#}"));
                        break;
                    }
                }
            }
            (times, receipts, errors, bytes)
        });
    }
    let mut times = Vec::new();
    let mut receipts = Vec::new();
    let mut errors = Vec::new();
    let mut bytes = 0;
    while let Some(result) = tasks.join_next().await {
        let (mut t, mut r, mut e, b) = result?;
        times.append(&mut t);
        receipts.append(&mut r);
        errors.append(&mut e);
        bytes += b;
    }
    let elapsed = start.elapsed().as_secs_f64();
    anyhow::ensure!(!times.is_empty(), "no successful responses: {errors:?}");
    times.sort_unstable();
    let result = serde_json::json!({
        "successful":times.len(), "elapsed_s":elapsed,
        "requests_per_s":times.len() as f64 / elapsed,
        "p50_ms":percentile(&times, 50), "p99_ms":percentile(&times, 99),
        "average_response_bytes":bytes as f64 / times.len() as f64,
        "receipts":receipts, "errors":errors,
    });
    std::fs::write(&args[5], serde_json::to_vec_pretty(&result)?)?;
    anyhow::ensure!(errors.is_empty(), "load errors: {errors:?}");
    Ok(())
}

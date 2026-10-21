use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::io::Write;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio_tungstenite::{connect_async, tungstenite::Message};

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    let a: Vec<String> = std::env::args().collect();
    anyhow::ensure!(
        a.len() == 7 || (a.len() == 8 && a[7] == "read-acks"),
        "usage: workers-chat-ws BASE ROOMS LISTENERS SECONDS INITIAL_VERSION OUTPUT [read-acks]"
    );
    let rooms: usize = a[2].parse()?;
    let listeners: usize = a[3].parse()?;
    let seconds: f64 = a[4].parse()?;
    let initial: u64 = a[5].parse()?;
    let read_acks = a.len() == 8;
    let participants = listeners + 1;
    let setup_versions = if read_acks { participants as u64 } else { 0 };
    let version_step = 1 + setup_versions;
    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let barriers: Vec<_> = (0..rooms)
        .map(|_| Arc::new(tokio::sync::Barrier::new(participants)))
        .collect();
    let base = a[1].replace("http://", "ws://");
    let mut readers = Vec::new();
    let mut writers = Vec::new();
    for room in 1..=rooms {
        for _ in 0..listeners {
            let (ws, _) = connect_async(format!("{base}/rooms/{room}/ws?token=token-2")).await?;
            let (w, r) = ws.split();
            readers.push((room, r));
            writers.push((room, w));
            if read_acks {
                let version = mark_read(&http, &a[1], room, 2).await?;
                anyhow::ensure!(
                    version == initial + writers.iter().filter(|(r, _)| *r == room).count() as u64,
                    "join read version differs"
                );
            }
        }
    }
    let (sent, rx) = tokio::sync::watch::channel(false);
    let mut reading = tokio::task::JoinSet::new();
    for (room, mut reader) in readers {
        let mut done = rx.clone();
        let http = http.clone();
        let endpoint = a[1].clone();
        let barrier = barriers[room - 1].clone();
        reading.spawn(async move{
            let mut versions=HashSet::new();let mut first=None;let mut last=None;
            let mut acknowledgments=Vec::new();
            loop{
                tokio::select!{
                    _=done.changed()=>break,
                    event=reader.next()=>{
                        let message=event.context("socket closed during broadcast")??;
                        if let Message::Text(t)=message{
                            let v:Value=serde_json::from_str(&t)?;
                            let version=v["version"].as_u64().context("broadcast has no version")?;
                            anyhow::ensure!(v["room_id"]==room && v["user_id"]==1 && v["body"]=="WebSocket pressure: 你好 👋 <b>& text</b>","broadcast content differs");
                            anyhow::ensure!(versions.insert(version),"duplicate broadcast");
                            if let Some(last)=last{anyhow::ensure!(version==last+version_step,"broadcast order changed");}
                            else{anyhow::ensure!(version==initial+setup_versions+1,"wrong initial broadcast version");first=Some(version);}
                            last=Some(version);
                            anyhow::ensure!(v["id"]==999+initial+versions.len() as u64 && v["created_at"]==1700000000+version-1,"broadcast row differs");
                            if read_acks {
                                let ack=mark_read(&http,&endpoint,room,2).await?;
                                anyhow::ensure!(ack>version && ack<=version+setup_versions,"read acknowledgment version differs");
                                acknowledgments.push(ack);
                                tokio::time::timeout(Duration::from_secs(30),barrier.wait()).await?;
                            }
                        }
                    }
                }
            }
            Ok::<_,anyhow::Error>((room,versions.len(),first,last,acknowledgments))
        });
    }
    let mut senders = Vec::new();
    for room in 1..=rooms {
        let (ws, _) = connect_async(format!("{base}/rooms/{room}/ws?token=token-1")).await?;
        senders.push((room, ws));
        if read_acks {
            let version = mark_read(&http, &a[1], room, 1).await?;
            anyhow::ensure!(
                version == initial + setup_versions,
                "sender join read version differs"
            );
        }
    }
    println!("{{\"ready\":true}}");
    std::io::stdout().flush()?;
    let mut go = String::new();
    std::io::stdin().read_line(&mut go)?;
    anyhow::ensure!(go.trim() == "go", "expected go on stdin");
    let start = Instant::now() + Duration::from_millis(50);
    let end = start + Duration::from_secs_f64(seconds);
    let mut sending = tokio::task::JoinSet::new();
    for (room, mut ws) in senders {
        let http = http.clone();
        let endpoint = a[1].clone();
        let barrier = barriers[room - 1].clone();
        sending.spawn(async move {
            tokio::time::sleep_until(tokio::time::Instant::from_std(start)).await;
            let mut count = 0u64;
            let mut latencies = Vec::new();
            let mut sender_latencies = Vec::new();
            let mut acknowledgments = Vec::new();
            while Instant::now() < end {
                let now = Instant::now();
                ws.send(Message::Text(
                    json!({"body":"WebSocket pressure: 你好 👋 <b>& text</b>"})
                        .to_string()
                        .into(),
                ))
                .await?;
                let response = tokio::time::timeout(Duration::from_secs(30), ws.next())
                    .await?
                    .context("sender disconnected")??;
                let v: Value = serde_json::from_str(response.to_text()?)?;
                count += 1;
                let version = initial + setup_versions + (count - 1) * version_step + 1;
                anyhow::ensure!(
                    v["version"] == version
                        && v["user_id"] == 1
                        && v["room_id"] == room
                        && v["id"] == 999 + initial + count
                        && v["created_at"] == 1700000000 + version - 1
                        && v["body"] == "WebSocket pressure: 你好 👋 <b>& text</b>",
                    "sender acknowledgment differs"
                );
                sender_latencies.push(now.elapsed().as_nanos() as u64);
                if read_acks {
                    let ack = mark_read(&http, &endpoint, room, 1).await?;
                    anyhow::ensure!(
                        ack > version && ack <= version + setup_versions,
                        "sender read version differs"
                    );
                    acknowledgments.push(ack);
                    tokio::time::timeout(Duration::from_secs(30), barrier.wait()).await?;
                }
                latencies.push(now.elapsed().as_nanos() as u64);
            }
            Ok::<_, anyhow::Error>((
                room,
                count,
                latencies,
                sender_latencies,
                acknowledgments,
                ws,
            ))
        });
    }
    let mut counts = vec![0; rooms];
    let mut latency = Vec::new();
    let mut sender_latency = Vec::new();
    let mut acknowledgment_versions: Vec<Vec<u64>> = vec![Vec::new(); rooms];
    let mut held = Vec::new();
    while let Some(r) = sending.join_next().await {
        let (room, count, mut times, mut sender_times, acknowledgments, ws) = r??;
        counts[room - 1] = count;
        latency.append(&mut times);
        sender_latency.append(&mut sender_times);
        acknowledgment_versions[room - 1].extend(acknowledgments);
        held.push(ws);
    }
    let elapsed = start.elapsed().as_secs_f64();
    tokio::time::sleep(Duration::from_millis(200)).await;
    sent.send(true)?;
    let mut delivered = 0;
    while let Some(r) = reading.join_next().await {
        let (room, count, _, last, acknowledgments) = r??;
        anyhow::ensure!(
            count as u64 == counts[room - 1]
                && last
                    == Some(initial + setup_versions + (counts[room - 1] - 1) * version_step + 1),
            "broadcast was lost: room {room}, {count} vs {}",
            counts[room - 1]
        );
        delivered += count;
        acknowledgment_versions[room - 1].extend(acknowledgments);
    }
    let mut read_ack_counts = Vec::new();
    for (room, versions) in acknowledgment_versions.iter_mut().enumerate() {
        versions.sort_unstable();
        if read_acks {
            let expected: Vec<_> = (0..counts[room])
                .flat_map(|message| {
                    let version = initial + setup_versions + message * version_step + 1;
                    (1..=setup_versions).map(move |offset| version + offset)
                })
                .collect();
            anyhow::ensure!(
                *versions == expected,
                "read acknowledgments lost or duplicated"
            );
        }
        read_ack_counts.push(versions.len());
    }
    for (_, mut w) in writers {
        let _ = w.close().await;
    }
    for mut ws in held {
        let _ = ws.close(None).await;
    }
    let total: u64 = counts.iter().sum();
    latency.sort_unstable();
    sender_latency.sort_unstable();
    let result = json!({"rooms":rooms,"listeners_per_room":listeners,"messages":total,"broadcast_deliveries":delivered,"total_frame_deliveries":delivered as u64+total,"elapsed_s":elapsed,"messages_per_s":total as f64/elapsed,"deliveries_per_s":(delivered as u64+total) as f64/elapsed,"p50_ms":latency[latency.len()/2]as f64/1e6,"p99_ms":latency[(latency.len()*99/100).min(latency.len()-1)]as f64/1e6,"sender_p99_ms":sender_latency[(sender_latency.len()*99/100).min(sender_latency.len()-1)]as f64/1e6,"write_counts":counts,"read_ack_counts":read_ack_counts,"setup_read_ack_counts":vec![setup_versions;rooms],"read_acknowledgments":read_ack_counts.iter().sum::<usize>(),"read_acknowledgments_enabled":read_acks,"errors":[]});
    std::fs::write(&a[6], serde_json::to_vec_pretty(&result)?)?;
    println!("{result}");
    Ok(())
}

async fn mark_read(client: &reqwest::Client, base: &str, room: usize, user: usize) -> Result<u64> {
    let response = client
        .post(format!("{base}/rooms/{room}/read"))
        .header("authorization", format!("Bearer token-{user}"))
        .send()
        .await?;
    if response.status() != reqwest::StatusCode::OK {
        anyhow::bail!(
            "read acknowledgment HTTP {}: {}",
            response.status(),
            response.text().await?
        );
    }
    let version = response
        .headers()
        .get("x-chat-version")
        .context("read acknowledgment has no version")?
        .to_str()?
        .parse()?;
    anyhow::ensure!(
        response.bytes().await?.as_ref() == b"{\"ok\":true}",
        "read acknowledgment body differs"
    );
    Ok(version)
}

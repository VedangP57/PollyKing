use anyhow::Result;
use log::{error, info};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

use crate::types::{ExecuteCommand, Gap, OrderPlaced};

pub async fn run(
    gap_rx: crossbeam_channel::Receiver<Gap>,
    order_tx: crossbeam_channel::Sender<(ExecuteCommand, Gap)>,
    pending_gaps: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, Gap>>>,
) -> Result<()> {
    let stdin = tokio::io::stdin();
    let stdout = tokio::io::stdout();
    let mut reader = BufReader::new(stdin);
    let mut writer = tokio::io::BufWriter::new(stdout);

    info!("Bridge started — reading commands from stdin, writing events to stdout");

    loop {
        // Drain gap channel and write to stdout
        while let Ok(gap) = gap_rx.try_recv() {
            let json = serde_json::to_string(&gap)?;
            writer.write_all(json.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;

            let mut map = pending_gaps.lock().unwrap();
            map.insert(gap.market_id.clone(), gap);
        }

        // Read one line from stdin (non-blocking via select!)
        let mut line = String::new();
        let bytes = tokio::time::timeout(
            std::time::Duration::from_millis(5),
            reader.read_line(&mut line),
        )
        .await;

        match bytes {
            Ok(Ok(0)) => {
                info!("stdin closed — bridge shutting down");
                break;
            }
            Ok(Ok(_)) => {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                match serde_json::from_str::<ExecuteCommand>(trimmed) {
                    Ok(cmd) => {
                        let map = pending_gaps.lock().unwrap();
                        // Find the most recent gap to associate with this command
                        if let Some(gap) = map.values().next().cloned() {
                            drop(map);
                            let _ = order_tx.try_send((cmd, gap));
                        }
                    }
                    Err(e) => {
                        error!("Bridge: failed to parse command: {e} | input: {trimmed}");
                    }
                }
            }
            Ok(Err(e)) => {
                error!("Bridge stdin error: {e}");
                break;
            }
            Err(_) => {
                // timeout — normal, loop again
            }
        }
    }

    Ok(())
}

pub async fn write_confirmation(confirmation: &OrderPlaced) -> Result<()> {
    let json = serde_json::to_string(confirmation)?;
    let mut stdout = tokio::io::stdout();
    stdout.write_all(json.as_bytes()).await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}

use anyhow::Result;
use log::info;
use tokio::io::AsyncWriteExt;

use crate::types::Gap;

pub async fn run(gap_rx: crossbeam_channel::Receiver<Gap>) -> Result<()> {
    let stdout = tokio::io::stdout();
    let mut writer = tokio::io::BufWriter::new(stdout);

    info!("Bridge started — writing gap events to stdout");

    loop {
        while let Ok(gap) = gap_rx.try_recv() {
            let json = serde_json::to_string(&gap)?;
            writer.write_all(json.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
        }
        tokio::time::sleep(tokio::time::Duration::from_millis(5)).await;
    }
}

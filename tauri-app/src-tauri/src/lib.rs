mod commands;
mod db;

use std::sync::Mutex;

use tauri::{image::Image, menu::{Menu, MenuItem, PredefinedMenuItem}, tray::TrayIconBuilder, Emitter, Manager};
use tokio::io::AsyncBufReadExt;

pub struct BotState {
    pub child: Option<std::process::Child>,
}

#[derive(serde::Serialize, Clone)]
struct PolykingMenuAction {
    action: &'static str,
}

fn tray_rgba_icon() -> Image<'static> {
    const W: u32 = 32;
    const H: u32 = 32;
    const PIXEL: [u8; 4] = [94, 92, 197, 255];
    let mut rgba = Vec::with_capacity((W * H * 4) as usize);
    for _ in 0..(W * H) {
        rgba.extend_from_slice(&PIXEL);
    }
    Image::new_owned(rgba, W, H)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .manage(Mutex::new(BotState { child: None }))
        .setup(|app| {
            let icon = tray_rgba_icon();

            let refresh_i = MenuItem::with_id(app, "refresh", "Refresh", true, None::<&str>)?;
            let start_i = MenuItem::with_id(app, "start", "Start Bot", true, None::<&str>)?;
            let stop_i = MenuItem::with_id(app, "stop", "Stop Bot", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit PolyyKing", true, None::<&str>)?;

            let tray_menu = Menu::with_items(
                app,
                &[&refresh_i, &start_i, &stop_i, &sep, &quit_i],
            )?;

            let _tray = TrayIconBuilder::with_id("main-tray")
                .icon(icon)
                .tooltip("PolyyKing")
                .menu(&tray_menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| {
                    if event.id.as_ref() == "quit" {
                        app.exit(0);
                        return;
                    }
                    let action = match event.id.as_ref() {
                        "refresh" => Some("refresh"),
                        "start" => Some("start"),
                        "stop" => Some("stop"),
                        _ => None,
                    };
                    if let (Some(a), Some(win)) = (action, app.get_webview_window("main")) {
                        let _ = win.emit(
                            "polyking-action",
                            PolykingMenuAction { action: a },
                        );
                    }
                })
                .build(app)?;

            // Unix socket: Python writes "gap" or "trade" to trigger a SQLite re-query
            // and a Tauri event push to the frontend (replaces 2s polling).
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let sock_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .parent().unwrap()
                    .parent().unwrap()
                    .join("data/polyking_events.sock");
                let _ = std::fs::remove_file(&sock_path);
                let listener = match tokio::net::UnixListener::bind(&sock_path) {
                    Ok(l) => l,
                    Err(e) => {
                        eprintln!("[PolyyKing] socket bind failed: {e}");
                        return;
                    }
                };
                loop {
                    let Ok((stream, _)) = listener.accept().await else {
                        continue;
                    };
                    let mut reader = tokio::io::BufReader::new(stream);
                    let mut line = String::new();
                    let n = reader.read_line(&mut line).await.unwrap_or(0);
                    if n == 0 {
                        continue;
                    }
                    let msg = line.trim();
                    let handle = app_handle.clone();
                    match msg {
                        "gap" => {
                            tokio::task::spawn_blocking(move || {
                                let gaps = db::get_active_gaps();
                                let _ = handle.emit("gap-detected", gaps);
                            });
                        }
                        "trade" => {
                            tokio::task::spawn_blocking(move || {
                                let trades = db::get_recent_trades();
                                let _ = handle.emit("trade-executed", trades);
                            });
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_bot_running,
            commands::get_mode,
            commands::set_dry_run,
            commands::get_ui_settings,
            commands::save_ui_settings,
            commands::get_daily_pnl,
            commands::tail_bot_log,
            commands::get_stats,
            commands::get_active_gaps,
            commands::get_recent_trades,
            commands::start_bot,
            commands::stop_bot,
            commands::get_risk_state,
            commands::get_calibration_stats,
            commands::get_portfolio_breakdown,
            commands::get_connection_status,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

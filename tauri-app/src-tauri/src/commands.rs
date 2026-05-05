use crate::db::{self, DailyPnl, Gap, Stats, Trade};
use crate::BotState;
use serde::{Deserialize, Serialize};
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use tauri::State;

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct UiSettings {
    pub gap_poll_ms: u64,
    pub trade_poll_ms: u64,
    pub mode_poll_ms: u64,
    pub notify_gap_min_cents: Option<f64>,
}

impl Default for UiSettings {
    fn default() -> Self {
        Self {
            gap_poll_ms: 2000,
            trade_poll_ms: 5000,
            mode_poll_ms: 3000,
            notify_gap_min_cents: None,
        }
    }
}

fn reconcile_bot_child(state: &mut BotState) {
    if let Some(ref mut c) = state.child {
        match c.try_wait() {
            Ok(Some(_)) => state.child = None,
            Ok(None) => {}
            Err(_) => {}
        }
    }
}

#[tauri::command]
pub fn get_bot_running(state: State<'_, Mutex<BotState>>) -> Result<bool, String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    reconcile_bot_child(&mut *s);
    Ok(s.child.is_some())
}

#[tauri::command]
pub fn get_mode() -> String {
    let env_path = find_env_path();
    if let Some(path) = env_path {
        if let Ok(content) = std::fs::read_to_string(&path) {
            for line in content.lines() {
                if line.starts_with("DRY_RUN=") {
                    let val = line.trim_start_matches("DRY_RUN=").trim().trim_matches('"');
                    if val.eq_ignore_ascii_case("false") {
                        return "LIVE".to_string();
                    }
                }
            }
        }
    }
    "DRY_RUN".to_string()
}

/// Persist `DRY_RUN=true|false` in the same `.env` file `get_mode` reads (creates file if missing and parent exists).
#[tauri::command]
pub fn set_dry_run(dry_run: bool) -> Result<(), String> {
    let root = project_root();
    let path = preferred_env_path(&root);
    let new_line = if dry_run {
        "DRY_RUN=true".to_string()
    } else {
        "DRY_RUN=false".to_string()
    };

    let mut content = if path.exists() {
        std::fs::read_to_string(&path).map_err(|e| e.to_string())?
    } else {
        String::new()
    };

    let mut found = false;
    let mut out_lines: Vec<String> = Vec::new();
    for line in content.lines() {
        if line.starts_with("DRY_RUN=") {
            out_lines.push(new_line.clone());
            found = true;
        } else {
            out_lines.push(line.to_string());
        }
    }
    if !found {
        out_lines.push(new_line);
    }
    content = out_lines.join("\n");
    if !content.ends_with('\n') {
        content.push('\n');
    }

    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, content).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn get_ui_settings() -> UiSettings {
    let path = settings_path();
    if let Ok(raw) = std::fs::read_to_string(&path) {
        if let Ok(s) = serde_json::from_str::<UiSettings>(&raw) {
            return s;
        }
    }
    UiSettings::default()
}

#[tauri::command]
pub fn save_ui_settings(settings: UiSettings) -> Result<(), String> {
    let path = settings_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let json = serde_json::to_string_pretty(&settings).map_err(|e| e.to_string())?;
    std::fs::write(&path, json + "\n").map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_daily_pnl(days: i64) -> Vec<DailyPnl> {
    db::get_daily_pnl(days)
}

#[tauri::command]
pub fn tail_bot_log(max_lines: usize) -> Result<String, String> {
    let path = project_root().join("data/bot.log");
    if !path.exists() {
        return Ok(String::new());
    }
    let max_lines = max_lines.clamp(1, 500);
    let file = OpenOptions::new()
        .read(true)
        .open(&path)
        .map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);
    let mut deque: std::collections::VecDeque<String> =
        std::collections::VecDeque::with_capacity(max_lines);
    for line in reader.lines().map_while(Result::ok) {
        if deque.len() == max_lines {
            deque.pop_front();
        }
        deque.push_back(line);
    }
    Ok(deque.into_iter().collect::<Vec<_>>().join("\n"))
}

#[tauri::command]
pub fn get_stats() -> Stats {
    db::get_stats()
}

#[tauri::command]
pub fn get_active_gaps() -> Vec<Gap> {
    db::get_active_gaps()
}

#[tauri::command]
pub fn get_recent_trades() -> Vec<Trade> {
    db::get_recent_trades()
}

#[tauri::command]
pub fn start_bot(state: State<'_, Mutex<BotState>>) -> Result<(), String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    reconcile_bot_child(&mut *s);
    if s.child.is_some() {
        return Ok(());
    }

    let python = find_python();
    let main_py = find_main_py();
    let root = project_root();
    let log_path = root.join("data/bot.log");
    let log_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .ok();

    let mut cmd = Command::new(&python);
    cmd.arg(&main_py)
        .current_dir(&root)
        .stdin(Stdio::null())
        .stdout(Stdio::null());
    if let Some(f) = log_file {
        cmd.stderr(f);
    } else {
        cmd.stderr(Stdio::null());
    }

    let child = cmd
        .spawn()
        .map_err(|e| format!("Failed to start bot: {e}"))?;

    s.child = Some(child);
    Ok(())
}

#[tauri::command]
pub fn stop_bot(state: State<'_, Mutex<BotState>>) -> Result<(), String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    reconcile_bot_child(&mut *s);
    if let Some(mut child) = s.child.take() {
        child.kill().map_err(|e| e.to_string())?;
        let _ = child.wait();
    }
    Ok(())
}

fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf()
}

fn preferred_env_path(root: &Path) -> PathBuf {
    let p = root.join("config/.env");
    if p.exists() {
        return p;
    }
    root.join(".env")
}

fn find_env_path() -> Option<String> {
    let root = project_root();
    let p = root.join("config/.env");
    if p.exists() {
        return Some(p.to_string_lossy().to_string());
    }
    let p2 = root.join(".env");
    if p2.exists() {
        return Some(p2.to_string_lossy().to_string());
    }
    None
}

fn settings_path() -> PathBuf {
    project_root().join("data/ui-settings.json")
}

fn find_python() -> String {
    let root = project_root();
    let venv = root.join("python-core/.venv/bin/python");
    if venv.exists() {
        return venv.to_string_lossy().to_string();
    }
    "python3".to_string()
}

fn find_main_py() -> String {
    let root = project_root();
    let main = root.join("python-core/main.py");
    main.to_string_lossy().to_string()
}

#[tauri::command]
pub fn get_risk_state() -> Result<db::RiskState, String> {
    db::get_risk_state(&db::db_path()).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_calibration_stats() -> Result<db::CalibrationStats, String> {
    db::get_calibration_stats(&db::db_path()).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_portfolio_breakdown() -> Result<Vec<db::CategoryBreakdown>, String> {
    db::get_portfolio_breakdown(&db::db_path()).map_err(|e| e.to_string())
}

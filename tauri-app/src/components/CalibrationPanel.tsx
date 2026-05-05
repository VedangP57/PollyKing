import { createQuery } from "@tanstack/solid-query";
import { invoke } from "@tauri-apps/api/core";
import { Show } from "solid-js";

interface CalibrationStats {
  brier_score: number | null;
  ev_error: number | null;
  win_rate: number | null;
  trade_count: number;
}

function fmt(v: number | null, decimals = 4): string {
  if (v === null || v === undefined) return "—";
  return v.toFixed(decimals);
}

export default function CalibrationPanel() {
  const calibQuery = createQuery<CalibrationStats>(() => ({
    queryKey: ["polyking", "calibration"],
    queryFn: () => invoke<CalibrationStats>("get_calibration_stats"),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: 0,
    refetchOnWindowFocus: false,
  }));

  const d = () => calibQuery.data;

  return (
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Calibration (30d live)</span>
        <span class="panel-count">{d()?.trade_count ?? 0} resolved</span>
      </div>
      <div class="stats-row" style="padding: 12px 16px;">
        <div class="stat-card">
          <div class="stat-label">Brier Score</div>
          <div class="stat-value">{fmt(d()?.brier_score ?? null)}</div>
          <div class="stat-sub">lower = better (0–1)</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">EV Error (MAE)</div>
          <div class="stat-value">${fmt(d()?.ev_error ?? null, 2)}</div>
          <div class="stat-sub">expected vs actual profit</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Win Rate</div>
          <div class={`stat-value ${(d()?.win_rate ?? 0) >= 0.5 ? "green" : ""}`}>
            {d()?.win_rate !== null && d()?.win_rate !== undefined
              ? `${((d()!.win_rate!) * 100).toFixed(1)}%`
              : "—"}
          </div>
          <div class="stat-sub">profitable resolved trades</div>
        </div>
        <Show when={!calibQuery.data && !calibQuery.isLoading}>
          <div class="empty-state">
            <div class="empty-dot" />
            <span>No resolved live trades yet</span>
          </div>
        </Show>
      </div>
    </div>
  );
}

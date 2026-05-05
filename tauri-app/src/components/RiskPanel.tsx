import { createQuery } from "@tanstack/solid-query";
import { invoke } from "@tauri-apps/api/core";
import { For, Show } from "solid-js";

interface RiskState {
  kill_switches: Record<string, boolean>;
  daily_loss_usdc: number;
  open_positions: number;
}

export default function RiskPanel() {
  const riskQuery = createQuery<RiskState>(() => ({
    queryKey: ["polyking", "riskState"],
    queryFn: () => invoke<RiskState>("get_risk_state"),
    staleTime: 15_000,
    refetchInterval: 15_000,
    retry: 0,
    refetchOnWindowFocus: false,
  }));

  const switches = () =>
    Object.entries(riskQuery.data?.kill_switches ?? {});

  return (
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Risk State</span>
      </div>
      <div class="panel-body" style="padding: 12px 16px; display: flex; gap: 24px; flex-wrap: wrap;">
        <div class="stat-card" style="min-width: 140px;">
          <div class="stat-label">Daily Loss</div>
          <div class={`stat-value ${(riskQuery.data?.daily_loss_usdc ?? 0) > 0 ? "profit-neg" : ""}`}>
            ${(riskQuery.data?.daily_loss_usdc ?? 0).toFixed(2)}
          </div>
        </div>
        <div class="stat-card" style="min-width: 140px;">
          <div class="stat-label">Open Positions</div>
          <div class="stat-value">{riskQuery.data?.open_positions ?? 0}</div>
        </div>
        <div style="flex: 1; min-width: 240px;">
          <div class="stat-label" style="margin-bottom: 8px;">Kill Switches</div>
          <Show when={switches().length > 0} fallback={<span class="dim">Loading…</span>}>
            <div style="display: flex; flex-direction: column; gap: 4px;">
              <For each={switches()}>
                {([name, active]) => (
                  <div style="display: flex; align-items: center; gap: 8px;">
                    <span
                      style={{
                        display: "inline-block",
                        width: "8px",
                        height: "8px",
                        "border-radius": "50%",
                        background: active ? "#ef4444" : "#22c55e",
                      }}
                    />
                    <span class="mono dim" style="font-size: 12px;">
                      {name.replace(/_/g, " ")}
                    </span>
                    <span
                      class={active ? "status-pill status-open" : "status-pill status-closed"}
                      style="font-size: 10px; padding: 1px 6px;"
                    >
                      {active ? "ACTIVE" : "ok"}
                    </span>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </div>
      </div>
    </div>
  );
}

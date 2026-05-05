import { createQuery } from "@tanstack/solid-query";
import { invoke } from "@tauri-apps/api/core";
import { For, Show } from "solid-js";

interface CategoryBreakdown {
  category: string;
  pnl: number;
  trade_count: number;
  win_rate: number;
}

function fmtPnl(v: number): string {
  return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
}

export default function PortfolioPanel() {
  const portfolioQuery = createQuery<CategoryBreakdown[]>(() => ({
    queryKey: ["polyking", "portfolio"],
    queryFn: () => invoke<CategoryBreakdown[]>("get_portfolio_breakdown"),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: 0,
    refetchOnWindowFocus: false,
  }));

  const rows = () => portfolioQuery.data ?? [];

  return (
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Portfolio (30d live)</span>
        <span class="panel-count">{rows().length} categories</span>
      </div>
      <div class="table-wrap">
        <Show
          when={rows().length > 0}
          fallback={
            <div class="empty-state">
              <div class="empty-dot" />
              <span>No live trade history yet</span>
            </div>
          }
        >
          <table>
            <caption class="sr-only">Portfolio breakdown by category</caption>
            <thead>
              <tr>
                <th style="width:30%">Category</th>
                <th class="right" style="width:25%">P&L</th>
                <th class="right" style="width:20%">Trades</th>
                <th class="right" style="width:25%">Win Rate</th>
              </tr>
            </thead>
            <tbody>
              <For each={rows()}>
                {(row) => (
                  <tr>
                    <td class="mono" style="text-transform: capitalize;">{row.category}</td>
                    <td class={`right mono ${row.pnl >= 0 ? "profit-pos" : "profit-neg"}`}>
                      {fmtPnl(row.pnl)}
                    </td>
                    <td class="right dim">{row.trade_count}</td>
                    <td class={`right mono ${row.win_rate >= 0.5 ? "profit-pos" : "profit-neg"}`}>
                      {(row.win_rate * 100).toFixed(1)}%
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </Show>
      </div>
    </div>
  );
}

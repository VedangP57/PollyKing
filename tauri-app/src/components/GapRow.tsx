import type { Component } from "solid-js";

export interface Gap {
  market_id: string;
  token_a_price: number;
  token_b_price: number;
  gap_cents: number;
  confidence: string;
  timestamp: number;
  outcome_count: number;
}

interface GapRowProps {
  gap: Gap;
  index: number;
  isNew: boolean;
  onCopy: (marketId: string) => void;
}

function fmtPrice(p: number): string {
  return (p * 100).toFixed(1) + "¢";
}

function fmtTime(ts: number): string {
  if (!ts) return "—"; // 0/null timestamp → em dash instead of epoch time
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function gapClass(gap: number): string {
  if (gap >= 5) return "gap-hot";
  if (gap >= 2) return "gap-warm";
  return "gap-cool";
}

function outcomeClass(count: number, marketId: string): string {
  if (!marketId.includes("::")) return "outcome-neutral";
  if (count === 2) return "outcome-ok";
  if (count === 0) return "outcome-unknown";
  return "outcome-warn";
}

function outcomeCell(count: number, marketId: string): string {
  // cross-platform pairs have no outcome_count (0) — show neutral
  if (!marketId.includes("::")) return "—";
  if (count === 2) return "✓";
  if (count === 0) return "?";
  return `⚠ ${count}`;
}

const GapRow: Component<GapRowProps> = (props) => {
  return (
    <tr>
      <td class="row-num">{props.index + 1}</td>
      <td>
        <button
          type="button"
          class="market-id market-id-btn"
          title="Copy market id"
          onClick={() => props.onCopy(props.gap.market_id)}
        >
          {props.gap.market_id}
          {props.isNew && <span class="badge-new">NEW</span>}
        </button>
      </td>
      <td class="right mono">{fmtPrice(props.gap.token_a_price)}</td>
      <td class="right mono">{fmtPrice(props.gap.token_b_price)}</td>
      <td class="right">
        <span class={`gap-pill ${gapClass(props.gap.gap_cents)}`}>
          {props.gap.gap_cents.toFixed(1)}¢
        </span>
      </td>
      <td class="dim">{props.gap.confidence}</td>
      <td
        class={outcomeClass(props.gap.outcome_count, props.gap.market_id)}
        title={
          props.gap.outcome_count > 0
            ? `${props.gap.outcome_count} outcomes in this event`
            : "Outcome count unavailable"
        }
      >
        {outcomeCell(props.gap.outcome_count, props.gap.market_id)}
      </td>
      <td class="right mono dim">{fmtTime(props.gap.timestamp)}</td>
    </tr>
  );
};

export default GapRow;

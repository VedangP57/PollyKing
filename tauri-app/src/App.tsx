import {
  createSignal,
  createEffect,
  onMount,
  onCleanup,
  untrack,
  For,
  Show,
  createMemo,
} from "solid-js";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";
import { Dialog, Tabs } from "@kobalte/core";
import { SolidUplot } from "@dschz/solid-uplot";
import {
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import { createShortcut } from "@solid-primitives/keyboard";
import { writeClipboard } from "@solid-primitives/clipboard";
import uPlot from "uplot";
import {
  RefreshCw,
  Settings as SettingsIcon,
  Play,
  Square,
  Loader2,
} from "lucide-solid";
import RiskPanel from "./components/RiskPanel";
import CalibrationPanel from "./components/CalibrationPanel";
import PortfolioPanel from "./components/PortfolioPanel";
import GapRow from "./components/GapRow";

interface Stats {
  pairs_count: number;
  gaps_today: number;
  trades_today: number;
  pnl: number;
  rejected_multi_outcome: number;
}

interface Gap {
  market_id: string;
  token_a_price: number;
  token_b_price: number;
  gap_cents: number;
  confidence: string;
  timestamp: number;
  outcome_count: number;
}

interface Trade {
  id: number;
  timestamp: number;
  market_id: string;
  side_a: string;
  side_b: string;
  expected_profit: number;
  status: string;
  dry_run: boolean;
}

interface ConnectionStatus { bot_running: boolean; ws_active: boolean }

interface UiSettings {
  gapPollMs: number;
  tradePollMs: number;
  modePollMs: number;
  notifyGapMinCents: number | null;
}

interface DailyPnl {
  date: string;
  pnl: number;
}

interface PolykingMenuPayload {
  action: "refresh" | "start" | "stop";
}

function isDebugLoggingEnabled(): boolean {
  return localStorage.getItem("pk_debug_logs") === "true";
}

function debugLog(message: string, data?: unknown): void {
  if (!isDebugLoggingEnabled()) return;
  if (data === undefined) {
    console.debug(`[PolyyKing UI] ${message}`);
    return;
  }
  console.debug(`[PolyyKing UI] ${message}`, data);
}

const DEFAULT_STATS: Stats = {
  pairs_count: 0,
  gaps_today: 0,
  trades_today: 0,
  pnl: 0,
  rejected_multi_outcome: 0,
};

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtPrice(p: number): string {
  return (p * 100).toFixed(1) + "¢";
}

function fmtPnl(v: number): string {
  return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
}

function errText(e: unknown): string {
  if (typeof e === "string") return e;
  if (e instanceof Error) return e.message;
  return "Something went wrong";
}

function clampSettings(s: UiSettings): UiSettings {
  return {
    gapPollMs: Math.min(120_000, Math.max(2_000, Number(s.gapPollMs) || 2_000)),
    tradePollMs: Math.min(300_000, Math.max(3_000, Number(s.tradePollMs) || 3_000)),
    modePollMs: Math.min(120_000, Math.max(3_000, Number(s.modePollMs) || 3_000)),
    notifyGapMinCents:
      s.notifyGapMinCents === null || s.notifyGapMinCents === undefined
        ? null
        : Number(s.notifyGapMinCents),
  };
}

const DEFAULT_SETTINGS: UiSettings = clampSettings({
  gapPollMs: 2000,
  tradePollMs: 3000,
  modePollMs: 3000,
  notifyGapMinCents: null,
});

type GapSortKey = "gap" | "time" | "market";
type TradeSortKey = "time" | "profit" | "market";
type SortDir = "asc" | "desc";

interface GapSort {
  key: GapSortKey;
  dir: SortDir;
}

interface TradeSort {
  key: TradeSortKey;
  dir: SortDir;
}

function toggleGapSort(prev: GapSort, key: GapSortKey): GapSort {
  const def: SortDir = key === "market" ? "asc" : "desc";
  if (prev.key !== key) return { key, dir: def };
  return { key, dir: prev.dir === "desc" ? "asc" : "desc" };
}

function toggleTradeSort(prev: TradeSort, key: TradeSortKey): TradeSort {
  const def: SortDir = key === "market" ? "asc" : "desc";
  if (prev.key !== key) return { key, dir: def };
  return { key, dir: prev.dir === "desc" ? "asc" : "desc" };
}

function sortGaps(list: Gap[], sort: GapSort): Gap[] {
  const out = [...list];
  const m = sort.dir === "asc" ? 1 : -1;
  out.sort((a, b) => {
    if (sort.key === "gap") return m * (a.gap_cents - b.gap_cents);
    if (sort.key === "time") return m * (a.timestamp - b.timestamp);
    return m * a.market_id.localeCompare(b.market_id);
  });
  return out;
}

function sortTrades(list: Trade[], sort: TradeSort): Trade[] {
  const out = [...list];
  const m = sort.dir === "asc" ? 1 : -1;
  out.sort((a, b) => {
    if (sort.key === "time") return m * (a.timestamp - b.timestamp);
    if (sort.key === "profit") return m * (a.expected_profit - b.expected_profit);
    return m * a.market_id.localeCompare(b.market_id);
  });
  return out;
}

function sortIndicator(sort: { key: string; dir: SortDir }, activeKey: string): string {
  if (sort.key !== activeKey) return "";
  return sort.dir === "desc" ? "↓" : "↑";
}

export default function App() {
  const sessionId = Math.random().toString(36).slice(2, 8);
  debugLog("App mounted", { sessionId, href: window.location.href });

  const onBeforeUnload = () => {
    debugLog("beforeunload fired");
  };
  window.addEventListener("beforeunload", onBeforeUnload);

  const onVisibilityChange = () => {
    debugLog("visibilitychange", { state: document.visibilityState });
  };
  document.addEventListener("visibilitychange", onVisibilityChange);
  onCleanup(() => {
    window.removeEventListener("beforeunload", onBeforeUnload);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  });

  const alertedGapKeys = new Set<string>();
  const qc = useQueryClient();

  const settingsQuery = createQuery(() => ({
    queryKey: ["polyking", "settings"],
    queryFn: async () => clampSettings(await invoke<UiSettings>("get_ui_settings")),
    staleTime: Infinity,
    retry: 1,
    refetchOnWindowFocus: false,
  }));

  const poll = () => settingsQuery.data ?? DEFAULT_SETTINGS;

  // Shared options applied to every polling query:
  // - retry:0       → on error, show stale data immediately; no retry storm
  // - staleTime     → don't re-fetch more often than the poll interval
  // - refetchOnWindowFocus:false → focusing the window doesn't fire extra reads
  const queryBase = () => ({
    enabled: () => settingsQuery.status === "success",
    retry: 0,
    refetchOnWindowFocus: false as const,
  });

  const statsQuery = createQuery(() => ({
    ...queryBase(),
    queryKey: ["polyking", "stats"],
    queryFn: () => invoke<Stats>("get_stats"),
    staleTime: 10_000,
    refetchInterval: 10_000,
  }));

  const connectionQuery = createQuery(() => ({
    ...queryBase(),
    queryKey: ["polyking", "connection"],
    queryFn: () => invoke<ConnectionStatus>("get_connection_status"),
    staleTime: 3_000,
    refetchInterval: 3_000,
  }));

  const modeQuery = createQuery(() => ({
    ...queryBase(),
    queryKey: ["polyking", "mode"],
    queryFn: () => invoke<string>("get_mode"),
    staleTime: poll().modePollMs,
    refetchInterval: poll().modePollMs,
  }));

  const botQuery = createQuery(() => ({
    ...queryBase(),
    queryKey: ["polyking", "botRunning"],
    queryFn: () => invoke<boolean>("get_bot_running"),
    staleTime: poll().tradePollMs,
    refetchInterval: poll().tradePollMs,
  }));

  const pnlQuery = createQuery(() => ({
    ...queryBase(),
    queryKey: ["polyking", "dailyPnl"],
    queryFn: () => invoke<DailyPnl[]>("get_daily_pnl", { days: 14 }),
    // P&L chart changes slowly — only refetch every 60 seconds
    staleTime: 60_000,
    refetchInterval: 60_000,
  }));

  const [errorMsg, setErrorMsg] = createSignal<string | null>(null);
  const [copyToast, setCopyToast] = createSignal<string | null>(null);
  const [settingsOpen, setSettingsOpen] = createSignal(false);
  const [liveConfirmOpen, setLiveConfirmOpen] = createSignal(false);
  const [settingsTab, setSettingsTab] = createSignal<"general" | "log">("general");

  const [draftGapMs, setDraftGapMs] = createSignal("2000");
  const [draftTradeMs, setDraftTradeMs] = createSignal("3000");
  const [draftModeMs, setDraftModeMs] = createSignal("3000");
  const [draftNotify, setDraftNotify] = createSignal("");
  const [logText, setLogText] = createSignal("");
  const [logLoading, setLogLoading] = createSignal(false);

  const [starting, setStarting] = createSignal(false);
  const [stopping, setStopping] = createSignal(false);

  const [gapSort, setGapSort] = createSignal<GapSort>({ key: "gap", dir: "desc" });
  const [tradeSort, setTradeSort] = createSignal<TradeSort>({ key: "time", dir: "desc" });

  const [gaps, setGaps] = createSignal<Gap[]>([]);
  const [trades, setTrades] = createSignal<Trade[]>([]);
  const [lastGapUpdate, setLastGapUpdate] = createSignal(Date.now());
  const [lastTradeUpdate, setLastTradeUpdate] = createSignal(Date.now());
  const [now, setNow] = createSignal(Date.now());

  const [gapFlash, setGapFlash] = createSignal(false);
  const [newGapIds, setNewGapIds] = createSignal(new Set<string>());

  let newGapTimer: ReturnType<typeof setTimeout> | undefined;
  let flashTimer: ReturnType<typeof setTimeout> | undefined;

  function triggerGapFlash() {
    clearTimeout(flashTimer);
    setGapFlash(true);
    flashTimer = setTimeout(() => setGapFlash(false), 300);
  }

  const [displayPnl, setDisplayPnl] = createSignal(0);

  createEffect(() => {
    const target = stats().pnl;
    const diff = target - untrack(() => displayPnl());
    if (Math.abs(diff) < 0.001) return;
    const step = diff / 20;
    let i = 0;
    const id = setInterval(() => {
      if (i++ >= 20) {
        clearInterval(id);
        setDisplayPnl(target);
        return;
      }
      setDisplayPnl((p) => p + step);
    }, 16);
    onCleanup(() => clearInterval(id));
  });

  const pnlCardClass = createMemo(() =>
    stats().pnl > 0 ? "stat-positive" : stats().pnl < 0 ? "stat-negative" : ""
  );

  const [tradesPulse, setTradesPulse] = createSignal(false);
  let prevTrades = -1;
  createEffect(() => {
    const count = stats().trades_today;
    if (prevTrades >= 0 && count > prevTrades) {
      setTradesPulse(true);
      const id = setTimeout(() => setTradesPulse(false), 600);
      onCleanup(() => clearTimeout(id));
    }
    prevTrades = count;
  });

  const connStatus = createMemo(() => connectionQuery.data);

  const [showChart, setShowChart] = createSignal(
    localStorage.getItem("pk_show_chart") === "true"
  );

  createEffect(() => {
    localStorage.setItem("pk_show_chart", String(showChart()));
  });

  const [showAnalytics, setShowAnalytics] = createSignal(
    localStorage.getItem("pk_show_analytics") === "true"
  );

  createEffect(() => {
    localStorage.setItem("pk_show_analytics", String(showAnalytics()));
  });

  const mode = createMemo(() =>
    modeQuery.data === "LIVE" ? "LIVE" : "DRY_RUN",
  );
  const running = createMemo(() => botQuery.data ?? false);
  const stats = createMemo(() => statsQuery.data ?? DEFAULT_STATS);

  const sortedGaps = createMemo(() => sortGaps(gaps(), gapSort()));
  const sortedTrades = createMemo(() => sortTrades(trades(), tradeSort()));

  const chartAligned = createMemo((): uPlot.AlignedData => {
    const rows = (pnlQuery.data ?? []) as DailyPnl[];
    if (rows.length === 0) return [[], []];
    const xs = rows.map((r) => new Date(r.date + "T12:00:00Z").getTime() / 1000);
    const ys = rows.map((r) => r.pnl);
    return [xs, ys];
  });

  const chartOptions = createMemo(
    (): Omit<uPlot.Options, "width" | "height" | "data"> => ({
      series: [
        {},
        {
          label: "P&L ($)",
          stroke: "#5e5cc5",
          width: 2,
          fill: "rgba(94,92,197,0.12)",
        },
      ],
      axes: [
        { stroke: "#737373", grid: { stroke: "#292929" } },
        {
          stroke: "#737373",
          grid: { stroke: "#292929" },
          values: (_u, vals) => vals.map((v) => "$" + Number(v).toFixed(0)),
        },
      ],
      scales: { x: { time: true }, y: {} },
    }),
  );

  const queryError = createMemo(() => {
    const candidates = [
      settingsQuery.error,
      statsQuery.error,
      connectionQuery.error,
      modeQuery.error,
      botQuery.error,
      pnlQuery.error,
    ];
    for (const e of candidates) {
      if (e) return errText(e);
    }
    return null;
  });

  const bannerText = createMemo(() => errorMsg() ?? queryError());

  createEffect(() => {
    const err = queryError();
    if (err) {
      debugLog("queryError", { err });
    }
  });

  const initialLoading = createMemo(() => settingsQuery.isPending);

  // Only show "syncing" if a fetch has been in-flight for >800ms.
  // Background polls complete in <100ms (WAL read), so this only fires
  // for genuine slowness — not every routine refetch cycle.
  const [slowSync, setSlowSync] = createSignal(false);
  createEffect(() => {
    const fetching =
      statsQuery.isFetching ||
      connectionQuery.isFetching ||
      modeQuery.isFetching ||
      botQuery.isFetching ||
      pnlQuery.isFetching;
    if (!fetching) {
      setSlowSync(false);
      return;
    }
    const t = setTimeout(() => setSlowSync(true), 800);
    onCleanup(() => clearTimeout(t));
  });
  const syncing = createMemo(() => slowSync());

  function invalidatePolyking() {
    debugLog("invalidatePolyking");
    void qc.invalidateQueries({ queryKey: ["polyking"] });
  }

  createShortcut(["Meta", "r"], () => void invalidatePolyking(), { preventDefault: true });
  createShortcut(["Control", "r"], () => void invalidatePolyking(), { preventDefault: true });
  createShortcut(["Meta", ","], () => openSettings(), { preventDefault: true });
  createShortcut(["Control", ","], () => openSettings(), { preventDefault: true });

  onMount(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);

    let unlistenGap: (() => void) | undefined;
    let unlistenTrade: (() => void) | undefined;

    onCleanup(() => {
      clearInterval(tickId);
      clearTimeout(newGapTimer);
      clearTimeout(flashTimer);
      unlistenGap?.();
      unlistenTrade?.();
    });

    // Hydrate on startup — populate signals before any live events arrive
    void invoke<Gap[]>("get_active_gaps").then((r) => {
      setGaps(r.slice(0, 50));
      setLastGapUpdate(Date.now());
    });
    void invoke<Trade[]>("get_recent_trades").then((r) => {
      setTrades(r.slice(0, 500));
      setLastTradeUpdate(Date.now());
    });

    void listen<Gap[]>("gap-detected", (e) => {
      setGaps(e.payload.slice(0, 50));
      setLastGapUpdate(Date.now());
      triggerGapFlash();
      const ids = new Set(e.payload.map((g) => g.market_id));
      setNewGapIds(ids);
      clearTimeout(newGapTimer);
      newGapTimer = setTimeout(() => setNewGapIds(new Set<string>()), 3000);
      void maybeNotifyForGaps(e.payload, poll());
    }).then((fn) => { unlistenGap = fn; });

    void listen<Trade[]>("trade-executed", (e) => {
      setTrades(e.payload.slice(0, 500));
      setLastTradeUpdate(Date.now());
    }).then((fn) => { unlistenTrade = fn; });
  });

  async function maybeNotifyForGaps(list: Gap[], cfg: UiSettings) {
    const thresh = cfg.notifyGapMinCents;
    if (thresh === null || Number.isNaN(thresh)) return;

    for (const g of list) {
      if (g.gap_cents < thresh) continue;
      const key = g.market_id;
      if (alertedGapKeys.has(key)) continue;

      let ok = await isPermissionGranted();
      if (!ok) {
        const p = await requestPermission();
        ok = p === "granted";
      }
      if (!ok) continue;

      alertedGapKeys.add(key);
      sendNotification({
        title: "PolyyKing — gap alert",
        body: `${g.market_id.slice(0, 48)}${g.market_id.length > 48 ? "…" : ""} · ${g.gap_cents.toFixed(1)}¢`,
      });
    }
  }

  createEffect(() => {
    let unlisten: UnlistenFn | undefined;
    void listen<PolykingMenuPayload>("polyking-action", (ev) => {
      const a = ev.payload.action;
      if (a === "refresh") void invalidatePolyking();
      else if (a === "start") void handleStart();
      else if (a === "stop") void handleStop();
    }).then((fn) => {
      unlisten = fn;
    });
    onCleanup(() => {
      unlisten?.();
    });
  });

  async function handleStart() {
    setStarting(true);
    try {
      await invoke("start_bot");
      setErrorMsg(null);
      await qc.invalidateQueries({ queryKey: ["polyking", "botRunning"] });
      await qc.invalidateQueries({ queryKey: ["polyking", "stats"] });
    } catch (e) {
      setErrorMsg(errText(e));
    }
    setStarting(false);
  }

  async function handleStop() {
    setStopping(true);
    try {
      await invoke("stop_bot");
      setErrorMsg(null);
      await qc.invalidateQueries({ queryKey: ["polyking", "botRunning"] });
      await qc.invalidateQueries({ queryKey: ["polyking", "stats"] });
    } catch (e) {
      setErrorMsg(errText(e));
    }
    setStopping(false);
  }

  async function switchToDry() {
    try {
      await invoke("set_dry_run", { dry_run: true });
      invalidatePolyking();
      setErrorMsg(null);
    } catch (e) {
      setErrorMsg(errText(e));
    }
  }

  async function confirmLiveMode() {
    try {
      await invoke("set_dry_run", { dry_run: false });
      setLiveConfirmOpen(false);
      invalidatePolyking();
      setErrorMsg(null);
    } catch (e) {
      setErrorMsg(errText(e));
    }
  }

  function openSettings() {
    const s = settingsQuery.data ?? DEFAULT_SETTINGS;
    setDraftGapMs(String(s.gapPollMs));
    setDraftTradeMs(String(s.tradePollMs));
    setDraftModeMs(String(s.modePollMs));
    setDraftNotify(
      s.notifyGapMinCents !== null && s.notifyGapMinCents !== undefined
        ? String(s.notifyGapMinCents)
        : "",
    );
    setSettingsTab("general");
    setSettingsOpen(true);
  }

  async function saveSettingsFromDraft() {
    const gap = Number.parseInt(draftGapMs(), 10);
    const trade = Number.parseInt(draftTradeMs(), 10);
    const modeMs = Number.parseInt(draftModeMs(), 10);
    if ([gap, trade, modeMs].some((n) => Number.isNaN(n))) {
      setErrorMsg("Polling intervals must be numbers.");
      return;
    }
    let notify: number | null = null;
    const raw = draftNotify().trim();
    if (raw !== "") {
      notify = Number.parseFloat(raw);
      if (Number.isNaN(notify)) {
        setErrorMsg("Notify threshold must be empty or a valid number (¢).");
        return;
      }
    }
    const next = clampSettings({
      gapPollMs: gap,
      tradePollMs: trade,
      modePollMs: modeMs,
      notifyGapMinCents: notify,
    });
    try {
      await invoke("save_ui_settings", { settings: next });
      qc.setQueryData(["polyking", "settings"], next);
      alertedGapKeys.clear();
      setSettingsOpen(false);
      setErrorMsg(null);
      void qc.invalidateQueries({ queryKey: ["polyking"] });
    } catch (e) {
      setErrorMsg(errText(e));
    }
  }

  async function loadBotLog() {
    setLogLoading(true);
    try {
      const text = await invoke<string>("tail_bot_log", { max_lines: 200 });
      setLogText(text || "");
    } catch (e) {
      setLogText(`(Could not read log: ${errText(e)})`);
    }
    setLogLoading(false);
  }

  async function copyMarketId(id: string) {
    try {
      await writeClipboard(id);
      setCopyToast("Copied");
      window.setTimeout(() => setCopyToast(null), 1600);
    } catch {
      setErrorMsg("Clipboard unavailable.");
    }
  }

  // Blacklisted event IDs — market_id for internal pairs starts with "{event_id}::"
  const BLACKLISTED_EVENT_IDS = new Set(["106231"]);

  function isBlacklisted(marketId: string): boolean {
    const eventId = marketId.split("::")[0];
    return BLACKLISTED_EVENT_IDS.has(eventId);
  }

  function outcomeCell(count: number, marketId: string): string {
    // cross-platform pairs have no outcome_count (0) — show neutral
    if (!marketId.includes("::")) return "—";
    if (count === 2) return "✓";
    if (count === 0) return "?";
    return `⚠ ${count}`;
  }

  function outcomeClass(count: number, marketId: string): string {
    if (!marketId.includes("::")) return "outcome-neutral";
    if (count === 2) return "outcome-ok";
    if (count === 0) return "outcome-unknown";
    return "outcome-warn";
  }

  function gapClass(gap: number): string {
    if (gap >= 5) return "gap-hot";
    if (gap >= 2) return "gap-warm";
    return "gap-cool";
  }

  function statusClass(s: string): string {
    if (s === "open") return "status-open";
    if (s === "closed") return "status-closed";
    if (s === "dry") return "status-dry";
    if (s === "profit") return "status-profit";
    return "status-closed";
  }

  function pnlClass(v: number): string {
    if (v > 0) return "profit-pos";
    if (v < 0) return "profit-neg";
    return "profit-zero";
  }

  function secsAgo(ts: number): string {
    const s = Math.floor((now() - ts) / 1000);
    if (s < 5) return "just now";
    return s + "s ago";
  }

  return (
    <div classList={{ shell: true, "chart-hidden": !showChart() }}>
      <div class={`error-banner ${bannerText() ? "" : "hidden"}`} role="alert">
        <span>{bannerText()}</span>
        <button type="button" onClick={() => setErrorMsg(null)}>
          Dismiss
        </button>
      </div>

      <header class="topbar">
        <div class="topbar-logo">
          Polyy<span>King</span>
        </div>

        <div class={`badge ${mode() === "LIVE" ? "badge-live" : "badge-dry"}`} aria-live="polite">
          <span class="badge-dot" aria-hidden />
          {mode() === "LIVE" ? "Live" : "Dry Run"}
        </div>

        <div class="mode-toggle" role="group" aria-label="Trading mode">
          <button
            type="button"
            classList={{ active: mode() === "DRY_RUN" }}
            aria-pressed={mode() === "DRY_RUN"}
            aria-label="Use dry run mode"
            onClick={() => {
              if (mode() === "DRY_RUN") return;
              void switchToDry();
            }}
          >
            Dry
          </button>
          <button
            type="button"
            classList={{ active: mode() === "LIVE" }}
            aria-pressed={mode() === "LIVE"}
            aria-label="Use live mode"
            onClick={() => {
              if (mode() === "LIVE") return;
              setLiveConfirmOpen(true);
            }}
          >
            Live
          </button>
        </div>

        <div class="topbar-spacer" />

        <Show when={copyToast()}>
          <span class="toast-inline" aria-live="polite">
            {copyToast()}
          </span>
        </Show>

        <span class={`topbar-status sync-indicator ${syncing() ? "syncing" : ""}`} aria-live="polite" style="display:flex;align-items:center;gap:6px;">
          <span style={{
            display: "inline-block",
            width: "9px",
            height: "9px",
            "border-radius": "50%",
            background: initialLoading() ? "#888" : running() ? "#22c55e" : "#ef4444",
            animation: running() ? "bot-pulse 1.8s infinite" : "none",
            "flex-shrink": "0",
          }} />
          {initialLoading()
            ? "loading…"
            : syncing()
              ? "syncing…"
              : running()
                ? "bot running"
                : "bot stopped"}
        </span>
        <span
          class={`conn-dot conn-dot-${
            !connStatus()?.bot_running
              ? "dead"
              : connStatus()?.ws_active
              ? "alive"
              : "degraded"
          }`}
          title="WS connection status"
        />

        <div class="topbar-actions">
          <button
            type="button"
            class="btn btn-ghost btn-with-icon"
            aria-label="Refresh all data"
            disabled={initialLoading()}
            onClick={() => void invalidatePolyking()}
          >
            <RefreshCw size={15} strokeWidth={2} />
            Refresh
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-with-icon"
            aria-label="Toggle P&L chart"
            onClick={() => setShowChart((v) => !v)}
          >
            {showChart() ? "Hide Chart" : "P&L Chart"}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-with-icon"
            aria-label="Toggle analytics panels"
            onClick={() => setShowAnalytics((v) => !v)}
          >
            {showAnalytics() ? "Hide Analytics" : "Analytics"}
          </button>
          <button type="button" class="btn btn-ghost btn-with-icon" aria-label="Open settings" onClick={openSettings}>
            <SettingsIcon size={15} strokeWidth={2} />
            Settings
          </button>
        </div>

        <Show when={!running()}>
          <button
            class="btn btn-start btn-with-icon"
            disabled={starting() || initialLoading()}
            aria-label="Start trading bot"
            onClick={() => void handleStart()}
          >
            <Show when={starting()} fallback={<Play size={15} strokeWidth={2} />}>
              <Loader2 size={15} strokeWidth={2} class="icon-spin" />
            </Show>
            {starting() ? "Starting…" : "Start"}
          </button>
        </Show>
        <Show when={running()}>
          <button
            class="btn btn-stop btn-with-icon"
            disabled={stopping()}
            aria-label="Stop trading bot"
            onClick={() => void handleStop()}
          >
            <Show when={stopping()} fallback={<Square size={15} strokeWidth={2} />}>
              <Loader2 size={15} strokeWidth={2} class="icon-spin" />
            </Show>
            {stopping() ? "Stopping…" : "Stop"}
          </button>
        </Show>
      </header>

      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-label">Pairs Monitored</div>
          <div class="stat-value accent">{stats().pairs_count.toLocaleString()}</div>
          <div class="stat-sub">market pairs</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Gaps Today</div>
          <div class="stat-value yellow">{stats().gaps_today}</div>
          <div class="stat-sub">arbitrage opportunities</div>
        </div>
        <div class={`stat-card ${tradesPulse() ? "stat-pulse" : ""}`}>
          <div class="stat-label">Trades Executed</div>
          <div class="stat-value">{stats().trades_today}</div>
          <div class="stat-sub">{mode() === "DRY_RUN" ? "simulated" : "live"}</div>
        </div>
        <div class={`stat-card ${pnlCardClass()}`}>
          <div class="stat-label">{mode() === "DRY_RUN" ? "Simulated P&L" : "Realized P&L"}</div>
          <div class={`stat-value ${stats().pnl >= 0 ? "green" : ""}`}>{fmtPnl(displayPnl())}</div>
          <div class="stat-sub">today</div>
          <Show when={stats().rejected_multi_outcome > 0}>
            <div class="stat-rejected">{stats().rejected_multi_outcome} pairs rejected (multi-outcome)</div>
          </Show>
        </div>
      </div>

      <div classList={{ "chart-row": true, hidden: !showChart() }} aria-label="Daily P and L history">
        <div class="chart-header">
          <span class="chart-title">14-day P&L (UTC)</span>
        </div>
        <div class="chart-body">
          <Show
            when={(pnlQuery.data ?? []).length > 0}
            fallback={<div class="chart-empty">No trade history to chart yet.</div>}
          >
            <div class="chart-uplot-wrap">
              <SolidUplot autoResize data={chartAligned()} {...chartOptions()} />
            </div>
          </Show>
        </div>
      </div>

      <Show when={showAnalytics()}>
        <RiskPanel />
        <CalibrationPanel />
        <PortfolioPanel />
      </Show>

      <div class="panel">
        <div classList={{ "panel-header": true, "panel-flash": gapFlash() }}>
          <span class="panel-title">Gaps Today</span>
          <span class="panel-count">{gaps().length}</span>
          <span class="panel-spacer" />
          <button
            type="button"
            class="btn btn-ghost panel-tool-btn btn-with-icon"
            aria-label="Refresh gaps"
            onClick={async () => {
              const result = await invoke<Gap[]>("get_active_gaps");
              setGaps(result.slice(0, 50));
              setLastGapUpdate(Date.now());
            }}
          >
            <RefreshCw size={14} strokeWidth={2} />
            Refresh
          </button>
          <span class="panel-refresh">
            {secsAgo(lastGapUpdate())}
          </span>
        </div>
        <div class="table-wrap">
          <Show
            when={gaps().length > 0}
            fallback={
              <div class="empty-state">
                <div class="empty-dot" />
                <span>
                  Watching {stats().pairs_count.toLocaleString()} pairs — no gaps detected today
                </span>
              </div>
            }
          >
            <table>
              <caption class="sr-only">Gap opportunities detected today</caption>
              <thead>
                <tr>
                  <th class="row-num-col" style="width:4%">#</th>
                  <th style="width:28%">
                    <button
                      type="button"
                      class="sort-btn"
                      onClick={() => setGapSort((p) => toggleGapSort(p, "market"))}
                    >
                      Market {sortIndicator(gapSort(), "market")}
                    </button>
                  </th>
                  <th class="right" style="width:10%">Token A</th>
                  <th class="right" style="width:10%">Token B</th>
                  <th class="right" style="width:10%">
                    <button
                      type="button"
                      class="sort-btn"
                      onClick={() => setGapSort((p) => toggleGapSort(p, "gap"))}
                    >
                      Gap {sortIndicator(gapSort(), "gap")}
                    </button>
                  </th>
                  <th style="width:10%">Confidence</th>
                  <th style="width:8%" title="Outcome count — must be 2 for safe arb">Outcomes</th>
                  <th class="right" style="width:20%">
                    <button
                      type="button"
                      class="sort-btn"
                      onClick={() => setGapSort((p) => toggleGapSort(p, "time"))}
                    >
                      Time {sortIndicator(gapSort(), "time")}
                    </button>
                  </th>
                </tr>
              </thead>
              <tbody>
                <For each={sortedGaps()}>
                  {(g, i) => (
                    <GapRow
                      gap={g}
                      index={i()}
                      isNew={newGapIds().has(g.market_id)}
                      onCopy={(id) => void copyMarketId(id)}
                    />
                  )}
                </For>
              </tbody>
            </table>
          </Show>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Recent Trades</span>
          <span class="panel-count">{trades().length}</span>
          <span class="panel-spacer" />
          <button
            type="button"
            class="btn btn-ghost panel-tool-btn btn-with-icon"
            aria-label="Refresh trades"
            onClick={async () => {
              const result = await invoke<Trade[]>("get_recent_trades");
              setTrades(result.slice(0, 500));
              setLastTradeUpdate(Date.now());
            }}
          >
            <RefreshCw size={14} strokeWidth={2} />
            Refresh
          </button>
          <span class="panel-refresh">
            {secsAgo(lastTradeUpdate())}
          </span>
        </div>
        <div class="table-wrap">
          <Show
            when={trades().length > 0}
            fallback={
              <div class="empty-state">
                <div class="empty-dot" />
                <span>No trades yet</span>
              </div>
            }
          >
            <table>
              <caption class="sr-only">Recent trades</caption>
              <thead>
                <tr>
                  <th class="row-num-col" style="width:4%">#</th>
                  <th style="width:12%">
                    <button
                      type="button"
                      class="sort-btn"
                      onClick={() => setTradeSort((p) => toggleTradeSort(p, "time"))}
                    >
                      Time {sortIndicator(tradeSort(), "time")}
                    </button>
                  </th>
                  <th style="width:28%">
                    <button
                      type="button"
                      class="sort-btn"
                      onClick={() => setTradeSort((p) => toggleTradeSort(p, "market"))}
                    >
                      Market {sortIndicator(tradeSort(), "market")}
                    </button>
                  </th>
                  <th style="width:10%">Side A</th>
                  <th style="width:10%">Side B</th>
                  <th class="right" style="width:12%">
                    <button
                      type="button"
                      class="sort-btn"
                      onClick={() => setTradeSort((p) => toggleTradeSort(p, "profit"))}
                    >
                      Exp. profit {sortIndicator(tradeSort(), "profit")}
                    </button>
                  </th>
                  <th style="width:10%">Status</th>
                </tr>
              </thead>
              <tbody>
                <For each={sortedTrades()}>
                  {(t, i) => {
                    const blacklisted = isBlacklisted(t.market_id);
                    return (
                      <tr classList={{ "row-blacklisted": blacklisted }}>
                        <td class="row-num">{i() + 1}</td>
                        <td class="mono dim">{fmtTime(t.timestamp)}</td>
                        <td>
                          <button
                            type="button"
                            class="market-id market-id-btn"
                            title="Copy market id"
                            onClick={() => void copyMarketId(t.market_id)}
                          >
                            {t.market_id}
                          </button>
                        </td>
                        <td class="dim">{t.side_a}</td>
                        <td class="dim">{t.side_b}</td>
                        <td class={`right mono ${pnlClass(t.expected_profit)}`}>{fmtPnl(t.expected_profit)}</td>
                        <td>
                          <Show
                            when={blacklisted}
                            fallback={
                              <span class={`status-pill ${statusClass(t.dry_run ? "dry" : t.status)}`}>
                                {t.dry_run ? "dry" : t.status}
                              </span>
                            }
                          >
                            <span class="status-pill status-blacklisted">BLACKLISTED</span>
                          </Show>
                        </td>
                      </tr>
                    );
                  }}
                </For>
              </tbody>
            </table>
          </Show>
        </div>
      </div>

      <Dialog.Root
        id="settings-dialog"
        open={settingsOpen()}
        onOpenChange={setSettingsOpen}
        modal
      >
        <Dialog.Portal>
          <Dialog.Overlay class="modal-backdrop k-dialog-overlay" />
          <Dialog.Content class="modal k-dialog-content" id="settings-dialog-content">
            <div class="modal-header">
              <Dialog.Title id="settings-dialog-title" class="modal-title">
                Settings
              </Dialog.Title>
              <Dialog.CloseButton class="modal-close" aria-label="Close settings">
                ×
              </Dialog.CloseButton>
            </div>

            <Tabs.Root
              value={settingsTab()}
              onChange={(v) => {
                const tab = v as "general" | "log";
                setSettingsTab(tab);
                if (tab === "log") void loadBotLog();
              }}
            >
              <Tabs.List class="modal-tabs">
                <Tabs.Trigger class="modal-tab" value="general">
                  General
                </Tabs.Trigger>
                <Tabs.Trigger class="modal-tab" value="log">
                  Bot log
                </Tabs.Trigger>
              </Tabs.List>

              <Tabs.Content class="modal-body" value="general">
                <div class="field">
                  <label for="gap-poll">Gaps poll (ms)</label>
                  <input
                    id="gap-poll"
                    type="number"
                    min={5000}
                    max={120000}
                    value={draftGapMs()}
                    onInput={(e) => setDraftGapMs(e.currentTarget.value)}
                  />
                  <div class="field-hint">How often the gaps table refreshes (5000–120000).</div>
                </div>
                <div class="field">
                  <label for="trade-poll">Trades poll (ms)</label>
                  <input
                    id="trade-poll"
                    type="number"
                    min={8000}
                    max={300000}
                    value={draftTradeMs()}
                    onInput={(e) => setDraftTradeMs(e.currentTarget.value)}
                  />
                </div>
                <div class="field">
                  <label for="mode-poll">Mode poll (ms)</label>
                  <input
                    id="mode-poll"
                    type="number"
                    min={8000}
                    max={120000}
                    value={draftModeMs()}
                    onInput={(e) => setDraftModeMs(e.currentTarget.value)}
                  />
                </div>
                <div class="field">
                  <label for="notify-gap">Notify when gap ≥ (¢)</label>
                  <input
                    id="notify-gap"
                    type="text"
                    placeholder="Leave empty to disable"
                    value={draftNotify()}
                    onInput={(e) => setDraftNotify(e.currentTarget.value)}
                  />
                  <div class="field-hint">Desktop notification once per gap row crossing this threshold.</div>
                </div>
                <div class="modal-footer">
                  <Dialog.CloseButton class="btn btn-ghost" aria-label="Cancel">
                    Cancel
                  </Dialog.CloseButton>
                  <button type="button" class="btn btn-primary" onClick={() => void saveSettingsFromDraft()}>
                    Save
                  </button>
                </div>
              </Tabs.Content>

              <Tabs.Content class="modal-body" value="log">
                <button type="button" class="btn btn-ghost btn-with-icon" disabled={logLoading()} onClick={() => void loadBotLog()}>
                  <RefreshCw size={14} strokeWidth={2} />
                  {logLoading() ? "Loading…" : "Reload log"}
                </button>
                <textarea class="log-view" readonly value={logText()} aria-label="Bot stderr log tail" />
              </Tabs.Content>
            </Tabs.Root>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        id="live-dialog"
        open={liveConfirmOpen()}
        onOpenChange={setLiveConfirmOpen}
        modal
      >
        <Dialog.Portal>
          <Dialog.Overlay class="modal-backdrop k-dialog-overlay" />
          <Dialog.Content class="modal k-dialog-content" id="live-dialog-content">
            <div class="modal-header">
              <Dialog.Title id="live-dialog-title" class="modal-title">
                Enable live trading?
              </Dialog.Title>
              <Dialog.CloseButton class="modal-close" aria-label="Close">
                ×
              </Dialog.CloseButton>
            </div>
            <div class="modal-body">
              <Dialog.Description id="live-dialog-desc" class="confirm-body">
                This updates <strong>DRY_RUN=false</strong> in your project <code>.env</code>. Only continue if you intend to place real orders.
              </Dialog.Description>
            </div>
            <div class="modal-footer">
              <Dialog.CloseButton class="btn btn-ghost">Cancel</Dialog.CloseButton>
              <button type="button" class="btn btn-stop" onClick={() => void confirmLiveMode()}>
                Switch to live
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
